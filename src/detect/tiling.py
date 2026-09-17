"""Tile geometry.

Pure arithmetic, no image library involved, so the geometry is testable on its
own and the same tiling is used by the detector and by anything that renders
tiles for review.

Tiles overlap, because a crack that falls on a tile boundary would otherwise be
split into two weak responses instead of one strong one.
"""

from __future__ import annotations

from dataclasses import dataclass

DEFAULT_TILE = 256
DEFAULT_OVERLAP = 0.25


@dataclass(frozen=True)
class Tile:
    """One tile's position in the original image's pixel coordinates."""

    index: int
    x: int
    y: int
    w: int
    h: int

    @property
    def box(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.x + self.w, self.y + self.h)


def plan_tiles(width: int, height: int, *, tile: int = DEFAULT_TILE,
               overlap: float = DEFAULT_OVERLAP) -> list[Tile]:
    """Lay out overlapping tiles covering the whole image.

    The last tile in each direction is pulled back flush with the edge rather
    than extended past it, so every tile is fully inside the image and no tile
    contains padding that a detector could mistake for content.

    Args:
        width, height: image size in pixels.
        tile: tile edge length. An image smaller than this yields a single tile
            covering it exactly.
        overlap: fraction of the tile edge shared with the next tile, in [0, 1).

    Raises:
        ValueError: on a non-positive size or an overlap outside [0, 1).
    """
    if width <= 0 or height <= 0:
        raise ValueError(f"image size must be positive, got {width}x{height}")
    if tile <= 0:
        raise ValueError(f"tile size must be positive, got {tile}")
    if not 0 <= overlap < 1:
        raise ValueError(f"overlap must be in [0, 1), got {overlap}")

    step = max(1, int(round(tile * (1 - overlap))))

    def offsets(extent: int) -> list[int]:
        if extent <= tile:
            return [0]
        positions = list(range(0, extent - tile + 1, step))
        if positions[-1] != extent - tile:
            positions.append(extent - tile)
        return positions

    tiles: list[Tile] = []
    index = 0
    for y in offsets(height):
        for x in offsets(width):
            tiles.append(Tile(index=index, x=x, y=y,
                              w=min(tile, width), h=min(tile, height)))
            index += 1
    return tiles
