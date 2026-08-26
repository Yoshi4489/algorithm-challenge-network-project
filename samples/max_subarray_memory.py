# Correct, O(1) *growth* in space — but a fixed ~96 MB block on every call. Verdict 603.
#
# This is the memory analogue of samples/max_subarray_busy_loop.py (602). Where 602 is the
# absolute-time ceiling (a run that takes too long) and 606 is the time *growth* violation
# (an algorithm one class too slow), the space axis splits the same way:
#
#   * 607 SPACE_COMPLEXITY_VIOLATION — the space *class* exceeds the contract. About growth.
#     samples/max_subarray_on_space.py is that case: O(n) auxiliary where O(1) is required.
#   * 603 MEMORY_LIMIT_EXCEEDED     — a single run's peak exceeds mem_limit_kb (64 MB here).
#     About an absolute ceiling, regardless of how it grows. This file.
#
# What makes this sample the clean 603 rather than a 607: its extra memory does NOT grow with
# n. It allocates the same ~96 MB block whether the input has 1,000 elements or 32,000, so
# the space-growth regression fits O(1) — *within* the contract. The growth check accepts it.
# The only thing that rejects it is the absolute limit: 96 MB > the 64 MB mem_limit_kb. That
# is exactly why 603 exists as a verdict distinct from 607 — constant space can still be too
# much space.
#
# How it is caught here. On Windows there is no resource.setrlimit and no cgroup, so the
# subprocess backend cannot cap memory at the OS level — the threat model names this openly
# as the backend's weakest point. What CDAP has instead is the in-child measurement: the
# runner's tracemalloc pass snapshots after the input is built, resets the peak, runs solve(),
# and reads the peak auxiliary allocation. For this solution that peak is ~96 MB at every
# size, and the profiler compares that absolute figure against the contract's mem_limit_kb.
# (The Docker backend is where a *real*, kernel-enforced --memory cap lives; that is the
# point of having both backends.)
#
# The solution is otherwise a correct Kadane, so it passes all ten correctness tests first —
# a memory verdict, like a complexity verdict, is only reached by a solution that is right.
# The junk block does no work; it exists solely to consume memory.
#
# Expected verdict: 603 MEMORY_LIMIT_EXCEEDED (tests 10/10, peak ~96 MB exceeds the 64 MB
# mem_limit_kb; space *growth* is O(1) but the absolute ceiling is the binding constraint).

#: 12 million pointers to the cached small int 0. A Python list's backing array is one
#: contiguous allocation of len*8 bytes on a 64-bit build, so this is a single ~96 MB block
#: that tracemalloc sees in full — comfortably above the 64 MB limit, with margin to spare.
HOG_ELEMENTS = 12_000_000


def solve(nums):
    # Allocate the block and keep a reference so it stays live across the measured call —
    # a block that were freed before solve() returns would not register as peak-time memory.
    hog = [0] * HOG_ELEMENTS
    # Touch it once so no interpreter version can optimise the allocation away as dead.
    hog[-1] = 1

    # The actual work: an ordinary O(n)/O(1) Kadane over the real input, so the answer is
    # correct and every test passes. The verdict comes from the block above, not from here.
    # Indexed rather than `nums[1:]` for the same reason as samples/max_subarray_on.py: a
    # slice is O(n) auxiliary space, and this sample's claim is that its extra space does
    # NOT grow with n. The 96 MB block would mask it either way, but the claim should be
    # true on its own terms.
    best = nums[0]
    running = nums[0]
    for i in range(1, len(nums)):
        value = nums[i]
        running = max(value, running + value)
        if running > best:
            best = running

    # Reference hog once more after the work so it cannot be collected mid-call.
    return best if hog[-1] == 1 else best
