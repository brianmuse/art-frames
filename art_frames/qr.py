"""A small QR code encoder for the Art Frames address: byte mode, error correction level M, versions 1 to 10.

It follows the QR code standard (ISO/IEC 18004) and the structure of Project Nayuki's public
QR code generator. Art Frames encodes only short URLs, so it needs no other modes or levels.
"""

from __future__ import annotations

from html import escape

# Level M, by version (index 0 unused).
ECC_PER_BLOCK = (-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26)
BLOCKS = (-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5)
MAX_VERSION = 10
FORMAT_LEVEL_M = 0


def _raw_modules(version):
    result = (16 * version + 128) * version + 64
    if version >= 2:
        count = version // 7 + 2
        result -= (25 * count - 10) * count - 55
        if version >= 7:
            result -= 36
    return result


def _data_codewords(version):
    return _raw_modules(version) // 8 - ECC_PER_BLOCK[version] * BLOCKS[version]


def _multiply(x, y):
    z = 0
    for i in reversed(range(8)):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _divisor(degree):
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _multiply(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _multiply(root, 0x02)
    return result


def _remainder(data, divisor):
    result = [0] * len(divisor)
    for byte in data:
        factor = byte ^ result.pop(0)
        result.append(0)
        for i, coefficient in enumerate(divisor):
            result[i] ^= _multiply(coefficient, factor)
    return result


def _alignment_positions(version):
    if version == 1:
        return []
    count = version // 7 + 2
    size = version * 4 + 17
    step = (version * 8 + count * 3 + 5) // (count * 4 - 4) * 2
    return [6] + sorted(size - 7 - i * step for i in range(count - 1))


def _codewords(data):
    for version in range(1, MAX_VERSION + 1):
        count_bits = 8 if version < 10 else 16
        if 4 + count_bits + 8 * len(data) <= _data_codewords(version) * 8:
            break
    else:
        raise ValueError("Text too long for this QR encoder")
    bits = []

    def put(value, length):
        bits.extend((value >> i) & 1 for i in reversed(range(length)))

    put(0b0100, 4)  # byte mode
    put(len(data), count_bits)
    for byte in data:
        put(byte, 8)
    capacity = _data_codewords(version) * 8
    put(0, min(4, capacity - len(bits)))
    put(0, -len(bits) % 8)
    pad = 0xEC
    while len(bits) < capacity:
        put(pad, 8)
        pad ^= 0xEC ^ 0x11
    codewords = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]

    # Split into blocks, add error correction, interleave.
    blocks_count, ecc_length = BLOCKS[version], ECC_PER_BLOCK[version]
    raw = _raw_modules(version) // 8
    short_blocks = blocks_count - raw % blocks_count
    short_length = raw // blocks_count
    divisor = _divisor(ecc_length)
    blocks, k = [], 0
    for i in range(blocks_count):
        length = short_length - ecc_length + (0 if i < short_blocks else 1)
        block = codewords[k:k + length]
        k += length
        ecc = _remainder(block, divisor)
        if i < short_blocks:
            block.append(0)
        blocks.append(block + ecc)
    result = []
    for i in range(len(blocks[0])):
        for j, block in enumerate(blocks):
            if i != short_length - ecc_length or j >= short_blocks:
                result.append(block[i])
    return version, result


class _Grid:
    def __init__(self, version):
        self.version = version
        self.size = version * 4 + 17
        self.dark = [[False] * self.size for _ in range(self.size)]
        self.fixed = [[False] * self.size for _ in range(self.size)]

    def set(self, x, y, dark):
        self.dark[y][x] = dark
        self.fixed[y][x] = True

    def function_patterns(self):
        size = self.size
        for i in range(size):
            self.set(6, i, i % 2 == 0)
            self.set(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < size and 0 <= y < size:
                        self.set(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        positions = _alignment_positions(self.version)
        last = len(positions) - 1
        for i, cx in enumerate(positions):
            for j, cy in enumerate(positions):
                if (i, j) in ((0, 0), (0, last), (last, 0)):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.set(cx + dx, cy + dy, max(abs(dx), abs(dy)) != 1)
        self.format_bits(0)
        if self.version >= 7:
            remainder = self.version
            for _ in range(12):
                remainder = (remainder << 1) ^ ((remainder >> 11) * 0x1F25)
            bits = self.version << 12 | remainder
            for i in range(18):
                bit = (bits >> i) & 1 == 1
                a, b = size - 11 + i % 3, i // 3
                self.set(a, b, bit)
                self.set(b, a, bit)

    def format_bits(self, mask):
        data = FORMAT_LEVEL_M << 3 | mask
        remainder = data
        for _ in range(10):
            remainder = (remainder << 1) ^ ((remainder >> 9) * 0x537)
        bits = (data << 10 | remainder) ^ 0x5412

        def bit(i):
            return (bits >> i) & 1 == 1

        size = self.size
        for i in range(6):
            self.set(8, i, bit(i))
        self.set(8, 7, bit(6))
        self.set(8, 8, bit(7))
        self.set(7, 8, bit(8))
        for i in range(9, 15):
            self.set(14 - i, 8, bit(i))
        for i in range(8):
            self.set(size - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self.set(8, size - 15 + i, bit(i))
        self.set(8, size - 8, True)

    def place(self, codewords):
        size, i, total = self.size, 0, len(codewords) * 8
        right = size - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vertical in range(size):
                for j in range(2):
                    x = right - j
                    upward = (right + 1) & 2 == 0
                    y = size - 1 - vertical if upward else vertical
                    if not self.fixed[y][x] and i < total:
                        self.dark[y][x] = (codewords[i >> 3] >> (7 - (i & 7))) & 1 == 1
                        i += 1
            right -= 2

    def apply_mask(self, mask):
        test = MASKS[mask]
        for y in range(self.size):
            for x in range(self.size):
                if not self.fixed[y][x] and test(x, y):
                    self.dark[y][x] = not self.dark[y][x]

    def penalty(self):
        """A score for how hard the code is to read; the encoder keeps the mask with the lowest."""
        size, dark, score = self.size, self.dark, 0
        for lines in (dark, list(zip(*dark, strict=True))):
            for line in lines:
                run, previous = 0, None
                for value in line:
                    run = run + 1 if value == previous else 1
                    previous = value
                    if run == 5:
                        score += 3
                    elif run > 5:
                        score += 1
                text = "".join("1" if value else "0" for value in line)
                score += 40 * (text.count("00001011101") + text.count("10111010000"))
        for y in range(size - 1):
            for x in range(size - 1):
                if dark[y][x] == dark[y][x + 1] == dark[y + 1][x] == dark[y + 1][x + 1]:
                    score += 3
        total = sum(map(sum, dark))
        score += abs(total * 20 - size * size * 10) // (size * size) * 10
        return score


MASKS = (
    lambda x, y: (x + y) % 2 == 0,
    lambda x, y: y % 2 == 0,
    lambda x, y: x % 3 == 0,
    lambda x, y: (x + y) % 3 == 0,
    lambda x, y: (x // 3 + y // 2) % 2 == 0,
    lambda x, y: x * y % 2 + x * y % 3 == 0,
    lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
    lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0,
)


def matrix(text):
    """The QR code for `text` as rows of booleans (True is dark), without the quiet zone."""
    version, codewords = _codewords(text.encode())
    best = None
    for mask in range(8):
        grid = _Grid(version)
        grid.function_patterns()
        grid.place(codewords)
        grid.apply_mask(mask)
        grid.format_bits(mask)
        score = grid.penalty()
        if best is None or score < best[0]:
            best = (score, grid)
    return best[1].dark


def terminal(text, quiet=2):
    """The QR code as text for a log, two rows of modules per line.

    Light modules print as blocks, so the code reads on a dark log screen, as most are.
    """
    rows = matrix(text)
    size = len(rows) + 2 * quiet
    padded = [[False] * size for _ in range(quiet)]
    padded += [[False] * quiet + row + [False] * quiet for row in rows]
    padded += [[False] * size for _ in range(quiet + 1)]
    lines = []
    for y in range(0, len(padded) - 1, 2):
        top, bottom = padded[y], padded[y + 1]
        line = "".join(
            " " if a and b else "▄" if a else "▀" if b else "█"
            for a, b in zip(top, bottom, strict=True)
        )
        lines.append(line)
    return "\n".join(lines)


def svg(text, quiet=4, title=""):
    """The QR code as an SVG image, black on white, with the standard quiet zone."""
    rows = matrix(text)
    size = len(rows) + 2 * quiet
    path = "".join(f"M{x + quiet},{y + quiet}h1v1h-1z" for y, row in enumerate(rows) for x, value in enumerate(row) if value)
    label = f"<title>{escape(title)}</title>" if title else ""
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" shape-rendering="crispEdges">{label}'
            f'<rect width="{size}" height="{size}" fill="#fff"/><path d="{path}" fill="#000"/></svg>')
