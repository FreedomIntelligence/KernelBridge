import math
import torch


THRESH = 16


def comp(a, b):
    av, _ = a
    bv, _ = b
    return (av < bv) or (not math.isnan(av) and math.isnan(bv))


def med(a, r, x, y, z):
    if comp(a[x], a[y]):
        q = y if comp(a[y], a[z]) else (z if comp(a[x], a[z]) else x)
    else:
        q = x if comp(a[x], a[z]) else (z if comp(a[y], a[z]) else y)
    a[r], a[q] = a[q], a[r]


def part(a, first, last, pivot):
    while True:
        while comp(a[first], a[pivot]):
            first += 1
        last -= 1
        while comp(a[pivot], a[last]):
            last -= 1
        if first >= last:
            return first
        a[first], a[last] = a[last], a[first]
        first += 1


def insertion(a, first, last):
    for i in range(first + 1, last):
        v = a[i]
        if comp(v, a[first]):
            for j in range(i, first, -1):
                a[j] = a[j - 1]
            a[first] = v
        else:
            j = i
            while comp(v, a[j - 1]):
                a[j] = a[j - 1]
                j -= 1
            a[j] = v


def final_insert(a):
    n = len(a)
    if n > THRESH:
        insertion(a, 0, THRESH)
        for i in range(THRESH, n):
            v = a[i]
            j = i
            while comp(v, a[j - 1]):
                a[j] = a[j - 1]
                j -= 1
            a[j] = v
    else:
        insertion(a, 0, n)


def push_heap(a, first, hole, top, value):
    parent = (hole - 1) // 2
    while hole > top and comp(a[first + parent], value):
        a[first + hole] = a[first + parent]
        hole = parent
        parent = (hole - 1) // 2
    a[first + hole] = value


def adjust_heap(a, first, hole, length, value):
    top = hole
    second = hole
    while second < (length - 1) // 2:
        second = 2 * (second + 1)
        if comp(a[first + second], a[first + second - 1]):
            second -= 1
        a[first + hole] = a[first + second]
        hole = second
    if length % 2 == 0 and second == (length - 2) // 2:
        second = 2 * (second + 1)
        a[first + hole] = a[first + second - 1]
        hole = second - 1
    push_heap(a, first, hole, top, value)


def heap_sort(a, first, last):
    length = last - first
    if length < 2:
        return
    parent = (length - 2) // 2
    while True:
        value = a[first + parent]
        adjust_heap(a, first, parent, length, value)
        if parent == 0:
            break
        parent -= 1
    while last - first > 1:
        last -= 1
        value = a[last]
        a[last] = a[first]
        adjust_heap(a, first, 0, last - first, value)


def mimic(x):
    a = [(float(v), i) for i, v in enumerate(x)]
    stack = [(0, len(a), 2 * int(math.log2(max(1, len(a)))))]
    while stack:
        first, last, depth = stack.pop()
        while last - first > THRESH:
            if depth == 0:
                heap_sort(a, first, last)
                break
            depth -= 1
            med(a, first, first + 1, first + (last - first) // 2, last - 1)
            cut = part(a, first + 1, last, first)
            stack.append((first, cut, depth))
            first = cut
    final_insert(a)
    return [i for _, i in a]


for n in [32, 997, 4096, 5000, 10291, 16129]:
    x = torch.arange(n, dtype=torch.int32).to(torch.float16)
    x[torch.arange(n) % 2 == 0] = float("nan")
    got = mimic(x.tolist())
    want = x.sort().indices.tolist()
    print(n, got == want, next((i for i, (a, b) in enumerate(zip(got, want)) if a != b), -1))
