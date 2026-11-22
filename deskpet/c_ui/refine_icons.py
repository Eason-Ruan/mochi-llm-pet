"""Original pixel icon paths from the Mochi Figma prototype."""

from functools import lru_cache

from PyQt6.QtCore import QByteArray, QRectF, Qt
from PyQt6.QtGui import QIcon, QPainter, QPixmap
from PyQt6.QtSvg import QSvgRenderer


_PATHS = {
    "bell": (
        15, 17, 1.41667,
        "M5.70825 15.7083H9.04159M2.37492 4.87501H4.04159V2.37501H5.70825V0.708344H9.04159V2.37501H10.7083V4.87501H12.3749V10.7083H14.0416V13.2083"
        "H0.708252V10.7083H2.37492V4.87501Z",
    ),
    "work": (
        14, 17, 1.41667,
        "M0.708252 0.708344H2.37492M2.37492 0.708344H10.7083M2.37492 0.708344V4.87501L6.54158 8.20834M10.7083 0.708344H12.3749M10.7083 0.708344"
        "V4.87501L6.54158 8.20834M0.708252 15.7083H2.37492M2.37492 15.7083H10.7083M2.37492 15.7083V11.5417L6.54158 8.20834M10.7083 15.7083H12.3749"
        "M10.7083 15.7083V11.5417L6.54158 8.20834M4.87492 13.2083H8.20825",
    ),
    "mic": (
        14, 17, 1.41667,
        "M0.708252 7.37501V10.7083L3.20825 13.2083H9.87492L12.3749 10.7083V7.37501M6.54159 13.2083V15.7083M3.20825 15.7083H9.87492M4.04159 2.37501"
        "V0.708344H9.04159V2.37501H9.87492V9.04168H8.20825V10.7083H4.87492V9.04168H3.20825V2.37501H4.04159Z",
    ),
    "settings": (
        17, 17, 1.375,
        "M6.52083 0.6875H9.85417V3.1875L11.5208 4.02083L14.0208 3.1875L15.6875 5.6875L14.0208 7.35417V9.02083L15.6875 10.6875L14.0208 13.1875"
        "L11.5208 12.3542L9.85417 13.1875V15.6875H6.52083V13.1875L4.85417 12.3542L2.35417 13.1875L0.6875 10.6875L2.35417 9.02083V7.35417"
        "L0.6875 5.6875L2.35417 3.1875L4.85417 4.02083L6.52083 3.1875V0.6875ZM6.52083 6.52083H9.85417V9.85417H6.52083V6.52083"
        "Z",
    ),
    "up": (
        14, 8, 1.41667,
        "M1.00171 6.54168L6.83504 0.708344L12.6684 6.54168",
    ),
    "down": (
        14, 8, 1.41667,
        "M1.00171 1.00174L6.83504 6.83507L12.6684 1.00174",
    ),
    "keyboard": (
        14, 10, 1.1,
        "M2.55005 3.21672H3.21672M5.21672 3.21672H5.88338M7.88338 3.21672H8.55005M10.55 3.21672H11.2167M3.21672 5.88338H4.55005M6.55005 5.88338"
        "H9.88338M0.550049 0.550049H12.55V8.55005H0.550049V0.550049Z",
    ),
    "back": (
        14, 12, 1.41667,
        "M13.2083 6.00171H0.708252M5.70825 11.0017L0.708252 6.00171L5.70825 1.00171",
    ),
    "send": (
        12, 10, 1.1,
        "M0.550049 4.77783H10.55M6.55005 8.77783L10.55 4.77783L6.55005 0.777832",
    ),
    "stop": (
        8, 8, 1.1,
        "M0.550049 0.550049H7.21672V7.21672H0.550049V0.550049Z",
    ),
    "link": (
        15, 15, 1.0625,
        "M5.625 5L7.5 3.125H10.625L11.875 4.375V6.875L10 8.75M5 6.25L3.125 8.125V10.625L4.375 11.875H7.5L9.375 10M5 10"
        "L10 5",
    ),
}


# The compact work control uses monochrome versions of the same pixel style.
width, height, stroke, hourglass = _PATHS["work"]
_PATHS.update({
    "work-focusing": (width, height, stroke, hourglass + "M5 12.5H8M5.75 11.75H7.25"),
    "work-paused": (14, 17, 2.1, "M3.5 3V14M9.5 3V14"),
    "work-review": (14, 17, 1.4, "M1.5 1.5H9L12 4.5V15.5H1.5ZM8.5 1.5V5H12M4 8H9.5M4 11.5H9.5"),
})


@lru_cache(maxsize=96)
def icon(name: str, color: str, size: int = 18) -> QIcon:
    """Keep the source viewbox ratio, and supply crisp native Retina variants."""
    width, height, stroke, path = _PATHS[name]
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}">'
           f'<path d="{path}" fill="none" stroke="{color}" stroke-width="{stroke}" '
           'stroke-linecap="square" stroke-linejoin="round"/></svg>')
    renderer = QSvgRenderer(QByteArray(svg.encode()))
    result = QIcon()
    for scale in (1, 2, 3):
        pixels = size * scale
        factor = pixels / max(width, height)
        w, h = width * factor, height * factor
        pixmap = QPixmap(pixels, pixels)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        renderer.render(painter, QRectF((pixels - w) / 2, (pixels - h) / 2, w, h))
        painter.end()
        pixmap.setDevicePixelRatio(scale)
        result.addPixmap(pixmap)
    return result
