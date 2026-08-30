def solve(nums: list[int]) -> int:
    """Return the maximum sum of a non-empty contiguous subarray.

    Kadane's algorithm examines every value exactly once and keeps only the best
    subarray ending at the current position.  The explicit empty-input failure is
    clearer than the accidental IndexError the previous implementation produced.
    """
    iterator = iter(nums)
    try:
        current = best = next(iterator)
    except StopIteration:
        raise ValueError("nums must contain at least one value") from None

    for value in iterator:
        extended = current + value
        current = value if value > extended else extended
        if current > best:
            best = current
    return best
