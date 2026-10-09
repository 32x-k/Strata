// Synthetic, low-cost tests for the bounded Q2_0 expert-RAM ring.
#include "strata/core/expert_source.hpp"
#include "strata/kernels/cpu/expert.hpp"
#include "strata/kernels/cpu/expert_layout.hpp"

#include <cuda_runtime.h>

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <filesystem>
#include <fstream>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

namespace fs = std::filesystem;
using strata::kernels::cpu::BLOB;

namespace {

void require(bool ok, const std::string& message) {
    if (!ok) throw std::runtime_error(message);
}

void check_cuda(cudaError_t status, const char* what) {
    if (status != cudaSuccess)
        throw std::runtime_error(std::string(what) + ": " + cudaGetErrorString(status));
}

struct TempDirectory {
    fs::path path;

    TempDirectory() {
        const auto stamp = std::chrono::steady_clock::now().time_since_epoch().count();
        path = fs::temp_directory_path() / ("strata-ring-source-test-" + std::to_string(stamp));
        fs::create_directories(path);
    }

    ~TempDirectory() {
        std::error_code ignored;
        fs::remove_all(path, ignored);
    }
};

void create_pack(const fs::path& dir, int64_t layers, int64_t experts) {
    std::ofstream out(dir / "experts.bin", std::ios::binary | std::ios::trunc);
    require((bool) out, "could not create synthetic experts.bin");
    std::vector<uint8_t> blob(BLOB);
    for (int64_t layer = 0; layer < layers; ++layer) {
        for (int64_t expert = 0; expert < experts; ++expert) {
            const uint8_t marker = (uint8_t) (0x20 + layer * experts + expert);
            std::fill(blob.begin(), blob.end(), marker);
            blob.back() = (uint8_t) (marker ^ 0xa5);
            out.write((const char*) blob.data(), (std::streamsize) blob.size());
        }
    }
    require((bool) out, "could not write synthetic experts.bin");
}

void check_blob(strata::core::RingExpertSource& source, int64_t layer, int64_t expert, uint8_t marker,
                const char* what) {
    const uint8_t* b = source.blob(layer, expert);
    require(b != nullptr, std::string(what) + ": blob lookup failed");
    require(b[0] == marker && b[BLOB - 1] == (uint8_t) (marker ^ 0xa5),
            std::string(what) + ": wrong bytes after eviction or reload");
}

void test_lru_and_same_layer_overflow(const fs::path& dir) {
    using strata::core::RingExpertSource;
    constexpr int64_t layers = 2, experts = 3;
    std::string err;
    require(strata::kernels::cpu::expert_layout_load(dir.string(), layers, experts, err),
            "could not load the canonical synthetic layout: " + err);

    RingExpertSource source;
    require(source.open(dir.string(), layers, experts, 2 * (uint64_t) BLOB, err),
            "could not open the synthetic ring: " + err);
    require(source.slots() == 2 && source.slot_bytes() == (int64_t) BLOB, "wrong synthetic ring geometry");
    require(source.transient() && source.device_alias(0, 0) == nullptr, "ring lifetime/alias contract is wrong");

    const int32_t ids[] = {0, 1, 2};
    source.begin_layer(0, ids, 3);
    check_blob(source, 0, 0, 0x20, "layer 0 expert 0");
    check_blob(source, 0, 1, 0x21, "layer 0 expert 1");
    require(source.blob(0, 2) == nullptr, "same-layer working-set overflow evicted a live slot");
    require(source.reads() == 2 && source.misses() == 4 && source.hits() == 2 && source.evictions() == 0 &&
                source.thrash() == 2,
            "same-layer overflow changed ring accounting");
    source.release_layer(0, nullptr);

    // The hit on expert 0 made slot 1 the LRU victim.  The next layer therefore evicts expert 1 first,
    // then expert 0, which distinguishes LRU from FIFO and proves that the old bytes are not returned.
    check_blob(source, 1, 0, 0x23, "layer 1 expert 0");
    check_blob(source, 1, 1, 0x24, "layer 1 expert 1");
    require(source.evictions() == 2 && source.reads() == 4, "LRU eviction accounting is wrong");
    source.release_layer(1, nullptr);

    std::vector<uint8_t> copied(BLOB, 0);
    require(source.copy_blob(0, 2, copied.data()), "copy_blob could not reload an evicted expert");
    require(copied[0] == 0x22 && copied.back() == (uint8_t) (0x22 ^ 0xa5),
            "copy_blob returned the wrong evicted expert");
    source.release_layer(0, nullptr);
}

struct CallbackGate {
    std::atomic<bool> started{false};
    std::atomic<bool> allow{false};
};

void test_fixed_verifier_reservation(const fs::path& dir) {
    using strata::core::RingExpertSource;
    constexpr int64_t layers = 2, experts = 3;
    std::string err;
    RingExpertSource source;
    source.set_verifier_reserve_slots(1);
    require(source.open(dir.string(), layers, experts, 2 * (uint64_t) BLOB, err),
            "could not open the verifier-reserved ring: " + err);
    require(source.slots() == 2 && source.normal_slots() == 1 && source.verifier_slots() == 1,
            "the fixed verifier reservation did not partition the ring");

    const int32_t normal_ids[] = {0};
    source.begin_layer(0, normal_ids, 1);
    const uint8_t* normal = source.blob(0, 0);
    require(normal == source.slot_host(0), "ordinary decode used the verifier-reserved slot");
    source.release_layer(0, nullptr);

    const int32_t verifier_ids[] = {1};
    require(source.begin_window(1, verifier_ids, 1), "a fitting verifier window was refused");
    const uint8_t* verifier = source.blob(1, 1);
    require(verifier == source.slot_host(1) && verifier[0] == 0x24,
            "the verifier window did not use its fixed tail slot");
    source.release_window(1);

    const int32_t too_many[] = {0, 1};
    require(!source.begin_window(1, too_many, 2), "an oversized verifier window exceeded its reservation");
    require(source.begin_window(1, verifier_ids, 1), "the verifier reservation was left dirty after refusal");
    source.release_window(1);

    const int32_t normal_again[] = {2};
    source.begin_layer(0, normal_again, 1);
    require(source.blob(0, 2) == source.slot_host(0),
            "ordinary decode borrowed the verifier tail after a verifier window");
    source.release_layer(0, nullptr);

    // The compatibility path without a reserved tail is transactional too: a failed multi-token reservation must
    // not strand the first normal slot and make the next window fail for an unrelated reason.
    RingExpertSource compatibility;
    require(compatibility.open(dir.string(), layers, experts, 2 * (uint64_t) BLOB, err),
            "could not open the compatibility ring");
    compatibility.set_verifier_reserve_slots(0);
    const int32_t overflow[] = {0, 1, 2};
    require(!compatibility.begin_window(0, overflow, 3), "the no-tail verifier path accepted an oversized window");
    const int32_t retry[] = {2};
    require(compatibility.begin_window(0, retry, 1), "the no-tail verifier rollback left a normal slot held");
    compatibility.release_window(0);
}

void test_batch_copy(const fs::path& dir) {
    using strata::core::RingExpertSource;
#if defined(_WIN32)
    const char* previous = std::getenv("STRATA_UNBUFFERED_LOAD");
    const bool had_previous = previous != nullptr;
    const std::string saved = previous != nullptr ? previous : "";
    require(_putenv_s("STRATA_UNBUFFERED_LOAD", "1") == 0, "could not force the Windows unbuffered test path");
#endif
    constexpr int64_t layers = 2, experts = 3;
    std::string err;
    RingExpertSource source;
    require(source.open(dir.string(), layers, experts, 4 * (uint64_t) BLOB, err),
            "could not open the batch-copy ring: " + err);
#if defined(_WIN32)
    if (had_previous) _putenv_s("STRATA_UNBUFFERED_LOAD", saved.c_str());
    else _putenv_s("STRATA_UNBUFFERED_LOAD", "");
    require(source.unbuffered(), "the forced Windows direct-I/O handle was not opened");
#endif

    const int32_t batch_layers[] = {0, 0, 0, 0};
    const int32_t batch_experts[] = {0, 1, 2, 1};
    std::vector<std::vector<uint8_t>> storage(4, std::vector<uint8_t>(BLOB));
    uint8_t* destinations[] = {storage[0].data(), storage[1].data(), storage[2].data(), storage[3].data()};
    require(source.copy_blobs(batch_layers, batch_experts, destinations, 4), "copy_blobs failed");
#if defined(_WIN32)
    require(source.unbuffered(), "copy_blobs fell back from unbuffered I/O");
#endif
    for (int i = 0; i < 4; ++i) {
        const uint8_t marker = (uint8_t) (0x20 + batch_experts[i]);
        require(storage[(size_t) i][0] == marker && storage[(size_t) i][BLOB - 1] == (uint8_t) (marker ^ 0xa5),
                "copy_blobs returned wrong bytes");
    }
    require(source.reads() == 3 && source.hits() == 1, "copy_blobs did not deduplicate the repeated expert");
    source.release_layer(0, nullptr);

    const int32_t prefetched[] = {0, 2};
    source.begin_layer(1, prefetched, 2);
#if defined(_WIN32)
    require(source.unbuffered(), "begin_layer fell back from unbuffered I/O");
#endif
    check_blob(source, 1, 0, 0x23, "batched begin_layer expert 0");
    check_blob(source, 1, 2, 0x25, "batched begin_layer expert 2");
    require(source.reads() == 5, "begin_layer did not load the two experts exactly once");
    source.release_layer(1, nullptr);
}

void test_access_trace(const fs::path& dir) {
    using strata::core::RingExpertSource;
    constexpr int64_t layers = 2, experts = 3;
    std::string err;
    RingExpertSource source;
    require(source.open(dir.string(), layers, experts, 2 * (uint64_t) BLOB, err),
            "could not open the access-trace ring: " + err);
    const fs::path trace_path = dir / "ring-access.jsonl";
    require(source.start_access_trace(trace_path.string(), err), "could not start the access trace: " + err);

    const int32_t ids[] = {0, 1};
    source.begin_layer(0, ids, 2);
    check_blob(source, 0, 0, 0x20, "traced layer 0 expert 0");
    source.release_layer(0, nullptr);
    source.access_trace_marker("decode_begin");
    const int32_t next[] = {2};
    source.begin_layer(1, next, 1);
    check_blob(source, 1, 2, 0x25, "traced layer 1 expert 2");
    source.release_layer(1, nullptr);
    source.access_trace_marker("decode_end");
    source.close();

    std::ifstream input(trace_path, std::ios::binary);
    require((bool) input, "could not read the access trace");
    const std::string text((std::istreambuf_iterator<char>(input)), std::istreambuf_iterator<char>());
    require(text.find("\"kind\":\"meta\"") != std::string::npos &&
                text.find("\"kind\":\"initial_lru\"") != std::string::npos,
            "the access trace is missing its metadata or initial cache state");
    require(text.find("\"kind\":\"access\"") != std::string::npos &&
                text.find("\"kind\":\"io_batch\"") != std::string::npos &&
                text.find("\"kind\":\"release\"") != std::string::npos,
            "the access trace is missing access, I/O, or release events");
    require(text.find("\"name\":\"decode_begin\"") != std::string::npos &&
                text.find("\"name\":\"decode_end\"") != std::string::npos &&
                text.find("\"kind\":\"summary\"") != std::string::npos,
            "the access trace is missing phase markers or its summary");
}

void test_event_release_and_final_layer(const fs::path& dir) {
    using strata::core::RingExpertSource;
    constexpr int64_t layers = 2, experts = 3;
    std::string err;
    RingExpertSource source;
    require(source.open(dir.string(), layers, experts, 2 * (uint64_t) BLOB, err),
            "could not reopen the synthetic ring: " + err);

    const int32_t ids[] = {0, 1};
    source.begin_layer(0, ids, 2);
    check_blob(source, 0, 0, 0x20, "event layer 0 expert 0");
    check_blob(source, 0, 1, 0x21, "event layer 0 expert 1");

    cudaStream_t stream = nullptr;
    cudaEvent_t event = nullptr;
    check_cuda(cudaStreamCreateWithFlags(&stream, cudaStreamNonBlocking), "cudaStreamCreateWithFlags");
    check_cuda(cudaEventCreateWithFlags(&event, cudaEventDisableTiming), "cudaEventCreateWithFlags");
    CallbackGate gate;
    check_cuda(cudaLaunchHostFunc(stream, [](void* p) {
        CallbackGate& g = *(CallbackGate*) p;
        g.started.store(true, std::memory_order_release);
        while (!g.allow.load(std::memory_order_acquire)) std::this_thread::yield();
    }, &gate), "cudaLaunchHostFunc");
    check_cuda(cudaEventRecord(event, stream), "cudaEventRecord");

    std::atomic<bool> released{false};
    std::thread releaser([&] {
        source.release_layer(0, (void*) event);
        released.store(true, std::memory_order_release);
    });
    std::this_thread::sleep_for(std::chrono::milliseconds(20));
    const bool released_early = released.load(std::memory_order_acquire);
    gate.allow.store(true, std::memory_order_release);
    releaser.join();
    require(gate.started.load(std::memory_order_acquire), "the event's host callback did not run");
    require(!released_early, "release_layer reused slots before the GPU event completed");

    check_blob(source, 1, 0, 0x23, "event layer 1 expert 0");
    source.release_layer(1, nullptr);  // final-layer release: the next token may start again at layer zero
    check_blob(source, 0, 2, 0x22, "after final-layer release");
    source.release_layer(0, nullptr);

    check_cuda(cudaEventDestroy(event), "cudaEventDestroy");
    check_cuda(cudaStreamDestroy(stream), "cudaStreamDestroy");
}

}  // namespace

int main() {
    int devices = 0;
    if (cudaGetDeviceCount(&devices) != cudaSuccess || devices == 0) return 77;

    try {
        constexpr int64_t layers = 2, experts = 3;
        TempDirectory dir;
        create_pack(dir.path, layers, experts);
        test_lru_and_same_layer_overflow(dir.path);
        test_fixed_verifier_reservation(dir.path);
        test_batch_copy(dir.path);
        test_access_trace(dir.path);
        test_event_release_and_final_layer(dir.path);
        std::puts("ring_expert_source_test: PASS");
        return 0;
    } catch (const std::exception& error) {
        std::fprintf(stderr, "ring_expert_source_test: %s\n", error.what());
        return 1;
    }
}
