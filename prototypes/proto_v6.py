import base64
import json
import os
import uuid
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple

from PySide6 import QtCore, QtGui, QtWidgets


# ============================================================
# Utilities
# ============================================================

def new_id() -> str:
    return uuid.uuid4().hex


def qcolor_to_rgba_tuple(c: QtGui.QColor) -> Tuple[int, int, int, int]:
    return (c.red(), c.green(), c.blue(), c.alpha())


def rgba_tuple_to_qcolor(rgba) -> QtGui.QColor:
    r, g, b, a = rgba
    return QtGui.QColor(r, g, b, a)


def qimage_to_png_base64(img: QtGui.QImage) -> str:
    buf = QtCore.QBuffer()
    buf.open(QtCore.QIODevice.WriteOnly)
    img.save(buf, "PNG")
    data = bytes(buf.data())
    return base64.b64encode(data).decode("ascii")


def png_base64_to_qimage(s: str) -> QtGui.QImage:
    data = base64.b64decode(s.encode("ascii"))
    img = QtGui.QImage()
    img.loadFromData(data, "PNG")
    return img


def clamp_rect_to_image(rect: QtCore.QRect, w: int, h: int) -> QtCore.QRect:
    return rect.intersected(QtCore.QRect(0, 0, w, h))


# ============================================================
# Data Model
# ============================================================

@dataclass
class Category:
    id: str
    name: str
    color: Tuple[int, int, int, int]  # RGBA
    index: int  # 1..255 (0 reserved for "none")


@dataclass
class Entity:
    id: str
    name: str
    category_id: Optional[str] = None
    props: dict = field(default_factory=dict)
    x: float = 0.0
    y: float = 0.0


@dataclass
class Layer:
    id: str
    name: str
    categories: List[Category] = field(default_factory=list)
    entities: List[Entity] = field(default_factory=list)
    # Persisted label map (Grayscale8) as PNG base64
    mask_index_png_b64: Optional[str] = None
    # Runtime caches (not in JSON)
    # mask_index_img: QtGui.QImage (Format_Grayscale8)
    # overlay_rgba_img: QtGui.QImage (Format_RGBA8888)
    # _lut_rgba: Optional[List[bytes]] cached LUT 0..255


@dataclass
class Project:
    image_path: Optional[str] = None
    image_width: int = 0
    image_height: int = 0
    layers: List[Layer] = field(default_factory=list)


# ============================================================
# Project Codec (JSON <-> Model), isolated from UI
# ============================================================

class ProjectCodec:
    @staticmethod
    def project_to_dict(project: Project) -> dict:
        return asdict(project)

    @staticmethod
    def dict_to_project(d: dict) -> Project:
        p = Project(
            image_path=d.get("image_path"),
            image_width=int(d.get("image_width", 0)),
            image_height=int(d.get("image_height", 0)),
            layers=[],
        )

        for ld in d.get("layers", []):
            layer = Layer(
                id=ld["id"],
                name=ld["name"],
                categories=[],
                entities=[],
                mask_index_png_b64=ld.get("mask_index_png_b64"),
            )

            # Assign indices if missing (older files)
            used = set()
            next_idx = 1

            for cd in ld.get("categories", []):
                idx = cd.get("index")
                if idx is None:
                    while next_idx in used and next_idx < 256:
                        next_idx += 1
                    idx = next_idx
                used.add(int(idx))

                layer.categories.append(
                    Category(
                        id=cd["id"],
                        name=cd["name"],
                        color=tuple(cd["color"]),
                        index=int(idx),
                    )
                )

            for ed in ld.get("entities", []):
                layer.entities.append(
                    Entity(
                        id=ed["id"],
                        name=ed["name"],
                        category_id=ed.get("category_id"),
                        props=ed.get("props", {}),
                        x=float(ed.get("x", 0.0)),
                        y=float(ed.get("y", 0.0)),
                    )
                )

            p.layers.append(layer)

        return p

    @staticmethod
    def load_json(path: str) -> Project:
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return ProjectCodec.dict_to_project(d)

    @staticmethod
    def save_json(path: str, project: Project) -> None:
        d = ProjectCodec.project_to_dict(project)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)


# ============================================================
# App State (selection + tool state) with signals
# ============================================================

class AppState(QtCore.QObject):
    selectionChanged = QtCore.Signal()
    toolChanged = QtCore.Signal()
    projectChanged = QtCore.Signal()

    def __init__(self, project: Optional[Project] = None):
        super().__init__()
        self.project: Project = project or Project()

        # Selection
        self.current_layer_id: Optional[str] = None
        self.current_category_id: Optional[str] = None
        self.current_entity_id: Optional[str] = None

        # Tool state
        self.tool_mode = "pan"  # pan | probe | brush | erase | entity_point
        self.brush_radius = 6
        self.probe_radius = 6
        self.erase_radius = 6
        self.erase_mode = "erase_all"  # erase_all | erase_only_category | erase_all_but_category

    # ----- selection helpers -----
    def set_layer(self, layer_id: Optional[str]):
        if self.current_layer_id == layer_id:
            return
        self.current_layer_id = layer_id
        self.current_category_id = None
        self.current_entity_id = None
        self.selectionChanged.emit()

    def set_category(self, category_id: Optional[str]):
        if self.current_category_id == category_id:
            return
        self.current_category_id = category_id
        self.selectionChanged.emit()

    def set_entity(self, entity_id: Optional[str]):
        if self.current_entity_id == entity_id:
            return
        self.current_entity_id = entity_id
        self.selectionChanged.emit()

    # ----- tool helpers -----
    def set_tool(self, tool: str):
        if self.tool_mode == tool:
            return
        self.tool_mode = tool
        self.toolChanged.emit()

    def notify_project_changed(self):
        self.projectChanged.emit()


# ============================================================
# Runtime stores / rendering / editing services (no QWidget here)
# ============================================================

class MaskStore:
    """Responsible for ensuring runtime mask images exist and are the right size."""

    def __init__(self, state: AppState):
        self.state = state

    def ensure_layer_index_mask(self, layer: Layer) -> QtGui.QImage:
        p = self.state.project
        w, h = p.image_width, p.image_height

        img = getattr(layer, "mask_index_img", None)
        if isinstance(img, QtGui.QImage) and not img.isNull():
            if img.width() == w and img.height() == h:
                return img

        # load once from b64
        if layer.mask_index_png_b64:
            loaded = png_base64_to_qimage(layer.mask_index_png_b64)
            if not loaded.isNull():
                loaded = loaded.convertToFormat(QtGui.QImage.Format_Grayscale8)
                if loaded.width() == w and loaded.height() == h:
                    layer.mask_index_img = loaded
                    return loaded

        blank = QtGui.QImage(w, h, QtGui.QImage.Format_Grayscale8)
        blank.fill(0)
        layer.mask_index_img = blank
        return blank

    def encode_all_masks_to_b64(self):
        """Call at save time only."""
        for layer in self.state.project.layers:
            img = getattr(layer, "mask_index_img", None)
            if isinstance(img, QtGui.QImage) and not img.isNull():
                layer.mask_index_png_b64 = qimage_to_png_base64(img.convertToFormat(QtGui.QImage.Format_Grayscale8))


class OverlayRenderer:
    """
    Builds/updates RGBA overlay image from index mask.

    Throttling: pixel writes happen immediately, but pushing the overlay into the
    QGraphicsPixmapItem (QPixmap.fromImage + setPixmap) is throttled to ~30fps.
    """

    def __init__(self, state: AppState, mask_store: MaskStore, scene: QtWidgets.QGraphicsScene):
        self.state = state
        self.mask_store = mask_store
        self.scene = scene
        self.layer_overlay_items: Dict[str, QtWidgets.QGraphicsPixmapItem] = {}

        # --- Throttle state ---
        self._pending_layers: set[str] = set()
        self._pending_rects: Dict[str, QtCore.QRect] = {}
        self._flush_timer = QtCore.QTimer()
        self._flush_timer.setSingleShot(True)
        self._flush_timer.setInterval(33)  # ~30fps
        self._flush_timer.timeout.connect(self._flush_pending_pixmaps)

    # ---- QImage bytes helpers ----
    @staticmethod
    def qimage_bytes(img: QtGui.QImage) -> memoryview:
        ptr = img.bits()
        if isinstance(ptr, memoryview):
            return ptr
        try:
            ptr.setsize(img.sizeInBytes())
        except AttributeError:
            return memoryview(ptr)
        return memoryview(ptr)

    def invalidate_lut(self, layer: Layer):
        if hasattr(layer, "_lut_rgba"):
            delattr(layer, "_lut_rgba")

    def category_lut_rgba(self, layer: Layer) -> List[bytes]:
        lut = getattr(layer, "_lut_rgba", None)
        if isinstance(lut, list) and len(lut) == 256:
            return lut

        lut = [b"\x00\x00\x00\x00"] * 256
        for c in layer.categories:
            r, g, b, a = c.color
            lut[c.index] = bytes((r, g, b, a))
        layer._lut_rgba = lut
        return lut

    def ensure_layer_overlay_image(self, layer: Layer) -> QtGui.QImage:
        p = self.state.project
        w, h = p.image_width, p.image_height

        rgba = getattr(layer, "overlay_rgba_img", None)
        if isinstance(rgba, QtGui.QImage) and not rgba.isNull():
            if rgba.width() == w and rgba.height() == h:
                return rgba

        rgba = QtGui.QImage(w, h, QtGui.QImage.Format_RGBA8888)
        rgba.fill(QtGui.QColor(0, 0, 0, 0))
        layer.overlay_rgba_img = rgba
        return rgba

    # -----------------------------
    # Throttle scheduling / flushing
    # -----------------------------

    def _schedule_pixmap_flush(self, layer: Layer, dirty_rect: Optional[QtCore.QRect] = None):
        """Mark layer as pending for pixmap push and start timer if not running."""
        lid = layer.id
        self._pending_layers.add(lid)

        if dirty_rect is not None:
            prev = self._pending_rects.get(lid)
            self._pending_rects[lid] = dirty_rect if prev is None else prev.united(dirty_rect)

        if not self._flush_timer.isActive():
            self._flush_timer.start()

    def flush_now(self, layer: Optional[Layer] = None):
        """
        Force immediate pixmap push:
        - If layer is given: flush only that layer
        - Else: flush all pending
        """
        if layer is not None:
            lid = layer.id
            # if not pending, still allow forcing a push
            self._push_pixmap_for_layer(layer)
            self._pending_layers.discard(lid)
            self._pending_rects.pop(lid, None)
            return

        # flush everything pending
        self._flush_pending_pixmaps()

    def _flush_pending_pixmaps(self):
        """Timer callback: push pending overlays into scene pixmaps."""
        if not self._pending_layers:
            return

        # Copy and clear first (so edits during flush schedule next run)
        lids = list(self._pending_layers)
        self._pending_layers.clear()
        self._pending_rects.clear()

        # Find current project layer objects by id and push pixmaps
        id_to_layer = {l.id: l for l in self.state.project.layers}
        for lid in lids:
            layer = id_to_layer.get(lid)
            if layer:
                self._push_pixmap_for_layer(layer)

    def _push_pixmap_for_layer(self, layer: Layer):
        """Actually do the expensive QPixmap.fromImage + setPixmap."""
        p = self.state.project
        if not p.image_path:
            return

        overlay_img = getattr(layer, "overlay_rgba_img", None)
        if not isinstance(overlay_img, QtGui.QImage) or overlay_img.isNull():
            return

        item = self.layer_overlay_items.get(layer.id)
        pm = QtGui.QPixmap.fromImage(overlay_img)

        if item is None:
            item = self.scene.addPixmap(pm)
            item.setZValue(10)
            item.setOpacity(0.55)
            item.setPos(0, 0)
            self.layer_overlay_items[layer.id] = item
        else:
            item.setPixmap(pm)

    # -----------------------------
    # Pixel updates (fast) + throttle
    # -----------------------------

    def update_layer_overlay(self, layer: Layer, dirty_rect: QtCore.QRect):
        """
        Updates overlay_rgba_img pixels immediately for dirty rect,
        but *throttles* pushing the pixmap into the scene.
        """
        p = self.state.project
        if not p.image_path:
            return

        mask = self.mask_store.ensure_layer_index_mask(layer)
        overlay = self.ensure_layer_overlay_image(layer)

        dr = clamp_rect_to_image(dirty_rect, p.image_width, p.image_height)
        if dr.isEmpty():
            return

        lut = self.category_lut_rgba(layer)
        mbytes = self.qimage_bytes(mask)
        obytes = self.qimage_bytes(overlay)

        for y in range(dr.top(), dr.bottom() + 1):
            mrow = y * mask.bytesPerLine()
            orow = y * overlay.bytesPerLine()
            x0 = dr.left()
            x1 = dr.right()

            mi = mrow + x0
            oi = orow + (x0 * 4)

            for _x in range(x0, x1 + 1):
                idx = mbytes[mi]
                rgba = lut[idx]
                obytes[oi:oi + 4] = rgba
                mi += 1
                oi += 4

        # Throttle pixmap updates
        self._schedule_pixmap_flush(layer, dr)

    # -----------------------------
    # Visibility / lifecycle
    # -----------------------------

    def hide_all_overlays(self):
        for item in self.layer_overlay_items.values():
            item.setVisible(False)

    def show_layer_overlay(self, layer: Optional[Layer]):
        self.hide_all_overlays()
        p = self.state.project
        if not layer or not p.image_path:
            return

        # ensure overlay exists once
        if not isinstance(getattr(layer, "overlay_rgba_img", None), QtGui.QImage):
            self.mask_store.ensure_layer_index_mask(layer)
            self.ensure_layer_overlay_image(layer)
            full = QtCore.QRect(0, 0, p.image_width, p.image_height)
            self.update_layer_overlay(layer, full)

        # ensure item exists (but don't spam updates)
        item = self.layer_overlay_items.get(layer.id)
        if item is None:
            # Force an immediate pixmap push once so it appears right away
            self._push_pixmap_for_layer(layer)
            item = self.layer_overlay_items.get(layer.id)

        if item:
            item.setVisible(True)

        # When switching layers, flush immediately for that layer so UI feels instant
        self.flush_now(layer)

    def clear_overlay_items(self):
        # stop timer and clear pending
        self._flush_timer.stop()
        self._pending_layers.clear()
        self._pending_rects.clear()

        for _lid, item in list(self.layer_overlay_items.items()):
            self.scene.removeItem(item)
        self.layer_overlay_items.clear()



class EditService:
    """All editing operations that mutate masks/entities/categories (no QWidget)."""

    def __init__(self, state: AppState, mask_store: MaskStore, overlay: OverlayRenderer):
        self.state = state
        self.mask_store = mask_store
        self.overlay = overlay

    def current_layer(self) -> Optional[Layer]:
        for l in self.state.project.layers:
            if l.id == self.state.current_layer_id:
                return l
        return None

    @staticmethod
    def qimage_bytes(img: QtGui.QImage) -> memoryview:
        return OverlayRenderer.qimage_bytes(img)

    # ---- paint / erase ----
    def paint_at(self, x: int, y: int) -> Optional[QtCore.QRect]:
        p = self.state.project
        layer = self.current_layer()
        if not layer or not self.state.current_category_id:
            return None

        cat = next((c for c in layer.categories if c.id == self.state.current_category_id), None)
        if not cat:
            return None

        mask = self.mask_store.ensure_layer_index_mask(layer)
        r = int(self.state.brush_radius)
        if r <= 0:
            return None

        mbytes = self.qimage_bytes(mask)
        bpl = mask.bytesPerLine()

        x0 = max(0, x - r)
        x1 = min(p.image_width - 1, x + r)
        y0 = max(0, y - r)
        y1 = min(p.image_height - 1, y + r)

        rr = r * r
        idx = cat.index

        for yy in range(y0, y1 + 1):
            dy = yy - y
            row = yy * bpl
            for xx in range(x0, x1 + 1):
                dx = xx - x
                if dx * dx + dy * dy <= rr:
                    mbytes[row + xx] = idx

        return QtCore.QRect(x0, y0, (x1 - x0 + 1), (y1 - y0 + 1))

    def erase_at(self, x: int, y: int) -> Optional[QtCore.QRect]:
        p = self.state.project
        layer = self.current_layer()
        if not layer:
            return None

        r = int(self.state.erase_radius)
        if r <= 0:
            return None

        mode = self.state.erase_mode

        selected_idx = None
        if mode in ("erase_only_category", "erase_all_but_category"):
            if not self.state.current_category_id:
                return None
            selected_cat = next((c for c in layer.categories if c.id == self.state.current_category_id), None)
            if not selected_cat:
                return None
            selected_idx = selected_cat.index

        mask = self.mask_store.ensure_layer_index_mask(layer)
        mbytes = self.qimage_bytes(mask)
        bpl = mask.bytesPerLine()

        x0 = max(0, x - r)
        x1 = min(p.image_width - 1, x + r)
        y0 = max(0, y - r)
        y1 = min(p.image_height - 1, y + r)

        rr = r * r

        for yy in range(y0, y1 + 1):
            dy = yy - y
            row = yy * bpl
            for xx in range(x0, x1 + 1):
                dx = xx - x
                if dx * dx + dy * dy > rr:
                    continue

                pos = row + xx
                cur = mbytes[pos]

                if mode == "erase_all":
                    if cur != 0:
                        mbytes[pos] = 0
                elif mode == "erase_only_category":
                    if cur == selected_idx:
                        mbytes[pos] = 0
                elif mode == "erase_all_but_category":
                    if cur != 0 and cur != selected_idx:
                        mbytes[pos] = 0

        return QtCore.QRect(x0, y0, (x1 - x0 + 1), (y1 - y0 + 1))

    # ---- probe ----
    def probe_at(self, x: int, y: int) -> Tuple[List[Tuple[float, str]], List[str]]:
        """
        Returns:
            category_results: list of (pct, name)
            entity_names: list of entity names in radius
        """
        p = self.state.project
        layer = self.current_layer()
        if not layer:
            return [], []

        r = int(self.state.probe_radius)
        if r <= 0:
            return [], []

        mask = self.mask_store.ensure_layer_index_mask(layer)
        mbytes = self.qimage_bytes(mask)
        bpl = mask.bytesPerLine()

        idx_to_name = {c.index: c.name for c in layer.categories}

        x0 = max(0, x - r)
        x1 = min(p.image_width - 1, x + r)
        y0 = max(0, y - r)
        y1 = min(p.image_height - 1, y + r)
        rr = r * r

        total = 0
        counts: Dict[int, int] = {}

        for yy in range(y0, y1 + 1):
            dy = yy - y
            row = yy * bpl
            for xx in range(x0, x1 + 1):
                dx = xx - x
                if dx * dx + dy * dy > rr:
                    continue
                total += 1
                idx = int(mbytes[row + xx])
                if idx != 0:
                    counts[idx] = counts.get(idx, 0) + 1

        results = []
        if total > 0:
            for idx, cnt in counts.items():
                pct = (cnt / total) * 100.0
                name = idx_to_name.get(idx, f"Unknown({idx})")
                if pct > 0.2:
                    results.append((pct, name))
            results.sort(reverse=True)

        ents_in = []
        for e in layer.entities:
            dx = e.x - x
            dy = e.y - y
            if dx * dx + dy * dy <= rr:
                ents_in.append(e.name)

        return results, ents_in

    # ---- entity placement ----
    def place_entity_point(self, x: int, y: int) -> bool:
        layer = self.current_layer()
        if not layer or not self.state.current_entity_id:
            return False
        ent = next((e for e in layer.entities if e.id == self.state.current_entity_id), None)
        if not ent:
            return False
        ent.x = x
        ent.y = y
        return True

    # ---- category deletion (v4 semantics: clear pixels for deleted index) ----
    def delete_category_and_clear_pixels(self, category_id: str) -> Optional[int]:
        """
        Deletes category from current layer, clears all pixels of its index in the layer mask,
        and detaches entities. Returns deleted index or None.
        """
        p = self.state.project
        layer = self.current_layer()
        if not layer:
            return None

        deleted = next((c for c in layer.categories if c.id == category_id), None)
        deleted_index = deleted.index if deleted else None

        layer.categories = [c for c in layer.categories if c.id != category_id]
        self.overlay.invalidate_lut(layer)

        # Detach entities from that category
        for e in layer.entities:
            if e.category_id == category_id:
                e.category_id = None

        if deleted_index is None or not p.image_path:
            return deleted_index

        # Full scan clear (expensive; matches your v4 semantics)
        mask = self.mask_store.ensure_layer_index_mask(layer)
        mbytes = self.qimage_bytes(mask)
        size = mask.sizeInBytes()
        for i in range(size):
            if mbytes[i] == deleted_index:
                mbytes[i] = 0

        return deleted_index

# ============================================================
# Undo / Redo (tile-based mask edits)
# ============================================================

class _StrokeMaskRecorder:
    """
    Records original bytes for tiles touched during a stroke, and can later
    snapshot "after" bytes for the same tiles.
    """
    def __init__(self, tile_size: int = 128):
        self.tile = int(tile_size)
        self.layer_id: Optional[str] = None
        self.tiles_before: Dict[Tuple[int, int], bytes] = {}
        self.tiles_rect: Dict[Tuple[int, int], QtCore.QRect] = {}
        self.dirty_union: Optional[QtCore.QRect] = None

    def begin(self, layer_id: str):
        self.layer_id = layer_id
        self.tiles_before.clear()
        self.tiles_rect.clear()
        self.dirty_union = None

    def is_active(self) -> bool:
        return self.layer_id is not None

    def _tile_rect(self, tx: int, ty: int, w: int, h: int) -> QtCore.QRect:
        x = tx * self.tile
        y = ty * self.tile
        return clamp_rect_to_image(QtCore.QRect(x, y, self.tile, self.tile), w, h)

    @staticmethod
    def _copy_rect_bytes(mask: QtGui.QImage, rect: QtCore.QRect) -> bytes:
        """Copy Grayscale8 pixels of rect into packed bytes (rect.w * rect.h)."""
        rect = rect.normalized()
        if rect.isEmpty():
            return b""

        bpl = mask.bytesPerLine()
        mv = OverlayRenderer.qimage_bytes(mask)

        out = bytearray(rect.width() * rect.height())
        out_i = 0

        for yy in range(rect.top(), rect.bottom() + 1):
            row = yy * bpl
            start = row + rect.left()
            end = start + rect.width()
            out[out_i:out_i + rect.width()] = mv[start:end]
            out_i += rect.width()

        return bytes(out)

    @staticmethod
    def _write_rect_bytes(mask: QtGui.QImage, rect: QtCore.QRect, data: bytes) -> None:
        rect = rect.normalized()
        if rect.isEmpty():
            return
        bpl = mask.bytesPerLine()
        mv = OverlayRenderer.qimage_bytes(mask)

        w = rect.width()
        expected = w * rect.height()
        if len(data) != expected:
            raise ValueError(f"rect bytes length mismatch: got {len(data)} expected {expected}")

        i = 0
        for yy in range(rect.top(), rect.bottom() + 1):
            row = yy * bpl
            start = row + rect.left()
            mv[start:start + w] = data[i:i + w]
            i += w

    def capture_before_for_rect(self, mask: QtGui.QImage, rect: QtCore.QRect, img_w: int, img_h: int):
        """
        Capture BEFORE bytes for any tiles overlapped by rect, but only once per tile.
        Must be called BEFORE you modify the mask in those tiles.
        """
        rect = clamp_rect_to_image(rect, img_w, img_h)
        if rect.isEmpty():
            return

        # Update union (used for overlay refresh)
        self.dirty_union = rect if self.dirty_union is None else self.dirty_union.united(rect)

        t = self.tile
        tx0 = rect.left() // t
        tx1 = rect.right() // t
        ty0 = rect.top() // t
        ty1 = rect.bottom() // t

        for ty in range(ty0, ty1 + 1):
            for tx in range(tx0, tx1 + 1):
                key = (tx, ty)
                if key in self.tiles_before:
                    continue
                tr = self._tile_rect(tx, ty, img_w, img_h)
                self.tiles_rect[key] = tr
                self.tiles_before[key] = self._copy_rect_bytes(mask, tr)

    def build_command(self, mask: QtGui.QImage, get_layer_fn, overlay, img_w: int, img_h: int):
        """
        Create an undo command from captured tiles. Returns None if nothing changed.
        """
        if not self.is_active() or not self.tiles_before:
            return None

        tiles_after: Dict[Tuple[int, int], bytes] = {}
        any_change = False
        for key, before in self.tiles_before.items():
            tr = self.tiles_rect[key]
            after = self._copy_rect_bytes(mask, tr)
            tiles_after[key] = after
            if after != before:
                any_change = True

        if not any_change:
            return None

        dirty = self.dirty_union or QtCore.QRect(0, 0, img_w, img_h)
        dirty = clamp_rect_to_image(dirty, img_w, img_h)

        return MaskTilesUndoCommand(
            layer_id=self.layer_id,
            tiles_rect=self.tiles_rect.copy(),
            tiles_before=self.tiles_before.copy(),
            tiles_after=tiles_after,
            dirty_rect=dirty,
            get_layer_fn=get_layer_fn,
            overlay=overlay,
        )


class MaskTilesUndoCommand(QtGui.QUndoCommand):
    """
    Undo/Redo for a stroke: restores tile bytes (before/after).
    """
    def __init__(
        self,
        layer_id: str,
        tiles_rect: Dict[Tuple[int, int], QtCore.QRect],
        tiles_before: Dict[Tuple[int, int], bytes],
        tiles_after: Dict[Tuple[int, int], bytes],
        dirty_rect: QtCore.QRect,
        get_layer_fn,
        overlay: "OverlayRenderer",
        description: str = "Stroke",
    ):
        super().__init__(description)
        self.layer_id = layer_id
        self.tiles_rect = tiles_rect
        self.tiles_before = tiles_before
        self.tiles_after = tiles_after
        self.dirty_rect = dirty_rect
        self.get_layer_fn = get_layer_fn
        self.overlay = overlay

    def _apply(self, which: str):
        layer = self.get_layer_fn(self.layer_id)
        if not layer:
            return

        # Ensure mask exists
        mask = self.overlay.mask_store.ensure_layer_index_mask(layer)

        # Apply tiles
        for key, tr in self.tiles_rect.items():
            data = self.tiles_before[key] if which == "before" else self.tiles_after[key]
            _StrokeMaskRecorder._write_rect_bytes(mask, tr, data)

        # Refresh overlay pixels + flush pixmap immediately (undo should feel instant)
        self.overlay.update_layer_overlay(layer, self.dirty_rect)
        self.overlay.flush_now(layer)

    def undo(self):
        self._apply("before")

    def redo(self):
        self._apply("after")




# ============================================================
# Views
# ============================================================

class ImageCanvas(QtWidgets.QGraphicsView):
    mouseMoved = QtCore.Signal(float, float)   # scene coords
    mouseClicked = QtCore.Signal(float, float)  # scene coords

    strokeStarted = QtCore.Signal(float, float)
    strokeMoved = QtCore.Signal(float, float)
    strokeEnded = QtCore.Signal(float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMouseTracking(True)
        self.setRenderHints(
            QtGui.QPainter.Antialiasing
            | QtGui.QPainter.SmoothPixmapTransform
            | QtGui.QPainter.TextAntialiasing
        )
        self.setDragMode(QtWidgets.QGraphicsView.NoDrag)
        self.setTransformationAnchor(QtWidgets.QGraphicsView.AnchorUnderMouse)

        # Middle mouse always-pan
        self._mm_panning = False
        self._saved_drag_mode = self.dragMode()
        self._saved_cursor = None

    def wheelEvent(self, event: QtGui.QWheelEvent):
        if event.modifiers() & QtCore.Qt.ControlModifier:
            delta = event.angleDelta().y()
            factor = 1.25 if delta > 0 else 0.8
            self.scale(factor, factor)
            event.accept()
            return
        super().wheelEvent(event)

    def mousePressEvent(self, event: QtGui.QMouseEvent):
        if event.button() == QtCore.Qt.MiddleButton:
            self._mm_panning = True
            self._saved_drag_mode = self.dragMode()
            self._saved_cursor = self.viewport().cursor()
            self.setDragMode(QtWidgets.QGraphicsView.ScrollHandDrag)
            self.viewport().setCursor(QtCore.Qt.ClosedHandCursor)

            fake = QtGui.QMouseEvent(
                event.type(),
                event.position(),
                event.globalPosition(),
                QtCore.Qt.LeftButton,
                QtCore.Qt.LeftButton,
                event.modifiers(),
            )
            super().mousePressEvent(fake)
            event.accept()
            return

        if event.button() == QtCore.Qt.LeftButton:
            p = self.mapToScene(event.position().toPoint())
            self.strokeStarted.emit(p.x(), p.y())
            # keep old click signal if you want single-click tools (probe/entity)
            self.mouseClicked.emit(p.x(), p.y())

        if event.button() == QtCore.Qt.LeftButton and self.dragMode() == QtWidgets.QGraphicsView.ScrollHandDrag:
            self.viewport().setCursor(QtCore.Qt.ClosedHandCursor)

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QtGui.QMouseEvent):
        p = self.mapToScene(event.position().toPoint())
        self.mouseMoved.emit(p.x(), p.y())

        # NEW: if left button is held, emit strokeMoved
        if event.buttons() & QtCore.Qt.LeftButton:
            # avoid interfering with middle-mouse panning
            if not getattr(self, "_mm_panning", False):
                self.strokeMoved.emit(p.x(), p.y())

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent):
        if event.button() == QtCore.Qt.MiddleButton and self._mm_panning:
            fake = QtGui.QMouseEvent(
                event.type(),
                event.position(),
                event.globalPosition(),
                QtCore.Qt.LeftButton,
                QtCore.Qt.NoButton,
                event.modifiers(),
            )
            super().mouseReleaseEvent(fake)

            self._mm_panning = False
            self.setDragMode(self._saved_drag_mode)
            if self._saved_cursor is not None:
                self.viewport().setCursor(self._saved_cursor)
            else:
                self.viewport().unsetCursor()

            event.accept()
            return

        if event.button() == QtCore.Qt.LeftButton:
            p = self.mapToScene(event.position().toPoint())
            self.strokeEnded.emit(p.x(), p.y())

        if self.dragMode() == QtWidgets.QGraphicsView.ScrollHandDrag:
            self.viewport().setCursor(QtCore.Qt.OpenHandCursor)

        super().mouseReleaseEvent(event)


class RightPanel(QtWidgets.QWidget):
    """Pure UI widget; emits signals and exposes widgets to controller."""

    addLayerClicked = QtCore.Signal()
    editLayerClicked = QtCore.Signal()
    deleteLayerClicked = QtCore.Signal()

    addCategoryClicked = QtCore.Signal()
    renameCategoryClicked = QtCore.Signal()
    deleteCategoryClicked = QtCore.Signal()

    addEntityClicked = QtCore.Signal()
    deleteEntityClicked = QtCore.Signal()
    applyEntityPropsClicked = QtCore.Signal()

    def __init__(self, parent=None):
        super().__init__(parent)

        self.layer_list = QtWidgets.QListWidget()
        self.category_list = QtWidgets.QListWidget()
        self.entity_list = QtWidgets.QListWidget()

        self.props_editor = QtWidgets.QPlainTextEdit()
        self.props_editor.setPlaceholderText('Entity properties JSON (e.g. {"type":"forest","owner":"..."} )')

        # tooling
        self.tool_stack = QtWidgets.QStackedWidget()
        self.spin_brush = QtWidgets.QSpinBox()
        self.spin_probe = QtWidgets.QSpinBox()
        self.spin_erase = QtWidgets.QSpinBox()
        self.combo_erase_mode = QtWidgets.QComboBox()

        self._build_ui()

    def _build_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        # Tooling pane
        tool_group = QtWidgets.QGroupBox("Tooling")
        tool_group_layout = QtWidgets.QVBoxLayout(tool_group)

        # Brush page
        brush_page = QtWidgets.QWidget()
        brush_layout = QtWidgets.QFormLayout(brush_page)
        brush_layout.setContentsMargins(0, 0, 0, 0)
        self.spin_brush.setRange(1, 100)
        brush_layout.addRow("Brush radius (px)", self.spin_brush)

        # Probe page
        probe_page = QtWidgets.QWidget()
        probe_layout = QtWidgets.QFormLayout(probe_page)
        probe_layout.setContentsMargins(0, 0, 0, 0)
        self.spin_probe.setRange(1, 100)
        probe_layout.addRow("Probe radius (px)", self.spin_probe)

        # Erase page
        erase_page = QtWidgets.QWidget()
        erase_layout = QtWidgets.QFormLayout(erase_page)
        erase_layout.setContentsMargins(0, 0, 0, 0)
        self.spin_erase.setRange(1, 100)
        erase_layout.addRow("Eraser radius (px)", self.spin_erase)

        self.combo_erase_mode.addItem("Erase all", "erase_all")
        self.combo_erase_mode.addItem("Erase only category", "erase_only_category")
        self.combo_erase_mode.addItem("Erase all but category", "erase_all_but_category")
        erase_layout.addRow("Mode", self.combo_erase_mode)

        # Empty page
        empty_page = QtWidgets.QWidget()
        empty_layout = QtWidgets.QVBoxLayout(empty_page)
        empty_layout.setContentsMargins(0, 0, 0, 0)
        empty_layout.addWidget(QtWidgets.QLabel("No parameters for this tool."))

        self.tool_stack.addWidget(empty_page)  # 0
        self.tool_stack.addWidget(brush_page)  # 1
        self.tool_stack.addWidget(probe_page)  # 2
        self.tool_stack.addWidget(erase_page)  # 3

        tool_group_layout.addWidget(self.tool_stack)
        layout.addWidget(tool_group)
        layout.addSpacing(6)

        # Layers
        layout.addWidget(QtWidgets.QLabel("Layers"))
        layout.addWidget(self.layer_list, 1)
        rowL = QtWidgets.QHBoxLayout()
        btn_add_layer = QtWidgets.QPushButton("Add layer")
        btn_edit_layer = QtWidgets.QPushButton("Edit layer")
        btn_del_layer = QtWidgets.QPushButton("Delete layer")
        btn_add_layer.clicked.connect(self.addLayerClicked.emit)
        btn_edit_layer.clicked.connect(self.editLayerClicked.emit)
        btn_del_layer.clicked.connect(self.deleteLayerClicked.emit)
        rowL.addWidget(btn_add_layer)
        rowL.addWidget(btn_edit_layer)
        rowL.addWidget(btn_del_layer)
        layout.addLayout(rowL)
        layout.addSpacing(6)

        # Categories
        layout.addWidget(QtWidgets.QLabel("Categories (per layer)"))
        layout.addWidget(self.category_list, 1)
        rowC = QtWidgets.QHBoxLayout()
        btn_add_cat = QtWidgets.QPushButton("Add category")
        btn_ren_cat = QtWidgets.QPushButton("Rename category")
        btn_del_cat = QtWidgets.QPushButton("Delete category")
        btn_add_cat.clicked.connect(self.addCategoryClicked.emit)
        btn_ren_cat.clicked.connect(self.renameCategoryClicked.emit)
        btn_del_cat.clicked.connect(self.deleteCategoryClicked.emit)
        rowC.addWidget(btn_add_cat)
        rowC.addWidget(btn_ren_cat)
        rowC.addWidget(btn_del_cat)
        layout.addLayout(rowC)
        layout.addSpacing(6)

        # Entities
        layout.addWidget(QtWidgets.QLabel("Entities (per layer)"))
        layout.addWidget(self.entity_list, 1)
        rowE = QtWidgets.QHBoxLayout()
        btn_add_ent = QtWidgets.QPushButton("Add entity (point)")
        btn_del_ent = QtWidgets.QPushButton("Delete entity")
        btn_add_ent.clicked.connect(self.addEntityClicked.emit)
        btn_del_ent.clicked.connect(self.deleteEntityClicked.emit)
        rowE.addWidget(btn_add_ent)
        rowE.addWidget(btn_del_ent)
        layout.addLayout(rowE)
        layout.addSpacing(6)

        # Props
        layout.addWidget(QtWidgets.QLabel("Selected entity properties (JSON)"))
        layout.addWidget(self.props_editor, 2)
        btn_apply_props = QtWidgets.QPushButton("Apply entity JSON")
        btn_apply_props.clicked.connect(self.applyEntityPropsClicked.emit)
        layout.addWidget(btn_apply_props)


# ============================================================
# Main Window / Controller
# ============================================================

class PixTagMainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()

        self.settings = QtCore.QSettings("PolyPixTagger", "PolyPixTagger")

        self.setWindowTitle("PixTag Prototype v4 (Refactored)")
        self.resize(1400, 900)

        # State
        self.state = AppState(Project())

        # Scene + canvas
        self.scene = QtWidgets.QGraphicsScene(self)
        self.canvas = ImageCanvas()
        self.canvas.setScene(self.scene)

        # Base image item
        self.base_pixmap_item: Optional[QtWidgets.QGraphicsPixmapItem] = None

        # Preview ring
        self.preview_ring = None
        self._last_mouse_scene_pos: Optional[Tuple[float, float]] = None
        self.ensure_preview_ring()

        # Services
        self.mask_store = MaskStore(self.state)
        self.overlay = OverlayRenderer(self.state, self.mask_store, self.scene)
        self.editor = EditService(self.state, self.mask_store, self.overlay)

        # Undo stack
        self.undo_stack = QtGui.QUndoStack(self)
        self._stroke_rec = _StrokeMaskRecorder(tile_size=128)

        # Right panel
        self.right = RightPanel()

        # UI layout
        self._build_layout()
        self._build_status_bar()
        self._build_actions()

        # Wire signals
        self._wire_events()

        # Ensure at least one layer
        self._ensure_default_layer()

        # Load last project if present
        QtCore.QTimer.singleShot(0, self.load_last_project_on_startup)

    # ---------------- layout ----------------

    def _build_layout(self):
        splitter = QtWidgets.QSplitter()
        splitter.addWidget(self.canvas)
        splitter.addWidget(self.right)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([800, 200])
        self.setCentralWidget(splitter)

    def _build_status_bar(self):
        self.status = self.statusBar()
        self.lbl_pos = QtWidgets.QLabel("x: -, y: -")
        self.lbl_tool = QtWidgets.QLabel("tool: pan")
        self.lbl_sel = QtWidgets.QLabel("layer: -, category: -, entity: -")
        self.status.addPermanentWidget(self.lbl_pos)
        self.status.addPermanentWidget(self.lbl_tool)
        self.status.addPermanentWidget(self.lbl_sel)

    # ---------------- wiring ----------------

    def _wire_events(self):
        # Canvas signals
        self.canvas.mouseMoved.connect(self.on_mouse_moved)
        self.canvas.mouseClicked.connect(self.on_mouse_clicked)

        # Stroke interpolation
        self.canvas.strokeStarted.connect(self.on_stroke_started)
        self.canvas.strokeMoved.connect(self.on_stroke_moved)
        self.canvas.strokeEnded.connect(self.on_stroke_ended)
        self._stroke_active = False
        self._stroke_last = None  # (x, y) in image coords

        # Right panel selection
        self.right.layer_list.currentItemChanged.connect(self._on_layer_item_changed)
        self.right.category_list.currentItemChanged.connect(self._on_category_item_changed)
        self.right.entity_list.currentItemChanged.connect(self._on_entity_item_changed)

        # Right panel buttons
        self.right.addLayerClicked.connect(self.add_layer)
        self.right.editLayerClicked.connect(self.edit_layer)
        self.right.deleteLayerClicked.connect(self.delete_layer)

        self.right.addCategoryClicked.connect(self.add_category)
        self.right.renameCategoryClicked.connect(self.rename_category)
        self.right.deleteCategoryClicked.connect(self.delete_category)

        self.right.addEntityClicked.connect(self.add_entity)
        self.right.deleteEntityClicked.connect(self.delete_entity)
        self.right.applyEntityPropsClicked.connect(self.apply_entity_props)

        # Tooling controls -> state
        self.right.spin_brush.valueChanged.connect(self._on_brush_radius_changed)
        self.right.spin_probe.valueChanged.connect(self._on_probe_radius_changed)
        self.right.spin_erase.valueChanged.connect(self._on_erase_radius_changed)
        self.right.combo_erase_mode.currentIndexChanged.connect(self._on_erase_mode_changed)

        # State signals -> UI refresh
        self.state.selectionChanged.connect(self.refresh_ui_from_state)
        self.state.toolChanged.connect(self.refresh_tool_ui)
        self.state.projectChanged.connect(self.refresh_ui_from_state)

    # ---------------- preview ring ----------------

    def ensure_preview_ring(self):
        if getattr(self, "preview_ring", None) is not None:
            try:
                self.preview_ring.isVisible()
                return
            except RuntimeError:
                pass

        self.preview_ring = QtWidgets.QGraphicsEllipseItem()
        pen = QtGui.QPen(QtGui.QColor(255, 255, 255, 220), 1)
        pen.setCosmetic(True)
        self.preview_ring.setPen(pen)
        self.preview_ring.setBrush(QtCore.Qt.NoBrush)
        self.preview_ring.setZValue(10_000)
        self.preview_ring.setVisible(False)
        self.scene.addItem(self.preview_ring)

    def update_tool_preview_ring(self, x: float, y: float):
        self.ensure_preview_ring()
        if self.state.tool_mode == "brush":
            r = self.state.brush_radius
        elif self.state.tool_mode == "erase":
            r = self.state.erase_radius
        elif self.state.tool_mode == "probe":
            r = self.state.probe_radius
        else:
            self.preview_ring.setVisible(False)
            return

        if r <= 0:
            self.preview_ring.setVisible(False)
            return

        self.preview_ring.setRect(x - r, y - r, 2 * r, 2 * r)
        self.preview_ring.setVisible(True)

    # ---------------- actions / menus / toolbar ----------------

    def _build_actions(self):
        # File actions
        self.act_new = QtGui.QAction(
            self.style().standardIcon(QtWidgets.QStyle.SP_FileDialogNewFolder),
            "New Image/Project",
            self,
        )
        self.act_new.triggered.connect(self.import_image)

        self.act_quick_save = QtGui.QAction(
            self.style().standardIcon(QtWidgets.QStyle.SP_DialogSaveButton),
            "Save Project",
            self,
        )
        self.act_quick_save.triggered.connect(self.quick_save_project_json)

        self.act_save_as = QtGui.QAction(
            self.style().standardIcon(QtWidgets.QStyle.SP_DialogSaveButton),
            "Save Project As",
            self,
        )
        self.act_save_as.triggered.connect(self.save_project_json)

        self.act_load = QtGui.QAction(
            self.style().standardIcon(QtWidgets.QStyle.SP_DialogOpenButton),
            "Load project JSON",
            self,
        )
        self.act_load.triggered.connect(self.load_project_json)

        self.act_quit = QtGui.QAction(
            self.style().standardIcon(QtWidgets.QStyle.SP_DialogCloseButton),
            "Quit",
            self,
        )
        self.act_quit.setShortcut(QtGui.QKeySequence.Quit)
        self.act_quit.triggered.connect(self.confirm_quit)

        # Undo/Redo actions (Ctrl+Z / Ctrl+Y)
        self.act_undo = self.undo_stack.createUndoAction(self, "Undo")
        self.act_undo.setShortcut(QtGui.QKeySequence.Undo)

        self.act_redo = self.undo_stack.createRedoAction(self, "Redo")
        self.act_redo.setShortcut(QtGui.QKeySequence.Redo)


        # Tools
        self.act_pan = QtGui.QAction("Pan", self, checkable=True)
        self.act_pan.setShortcut(QtGui.QKeySequence("Ctrl+M"))
        self.act_pan.triggered.connect(lambda: self.state.set_tool("pan"))

        self.act_probe = QtGui.QAction("Probe", self, checkable=True)
        self.act_probe.setShortcut(QtGui.QKeySequence("Ctrl+I"))
        self.act_probe.triggered.connect(lambda: self.state.set_tool("probe"))

        self.act_erase = QtGui.QAction("Erase", self, checkable=True)
        self.act_erase.setShortcut(QtGui.QKeySequence("Ctrl+E"))
        self.act_erase.setShortcutContext(QtCore.Qt.ApplicationShortcut)
        self.act_erase.triggered.connect(lambda: self.state.set_tool("erase"))

        self.act_brush = QtGui.QAction("Brush", self, checkable=True)
        self.act_brush.setShortcut(QtGui.QKeySequence("Ctrl+B"))
        self.act_brush.triggered.connect(lambda: self.state.set_tool("brush"))

        self.act_entity_point = QtGui.QAction("Point", self, checkable=True)
        self.act_entity_point.setShortcut(QtGui.QKeySequence("Ctrl+P"))
        self.act_entity_point.triggered.connect(lambda: self.state.set_tool("entity_point"))

        # Menus
        menu = self.menuBar()
        m_file = menu.addMenu("File")
        m_file.addAction(self.act_new)
        m_file.addAction(self.act_load)
        m_file.addAction(self.act_quick_save)
        m_file.addAction(self.act_save_as)
        m_file.addSeparator()
        m_file.addAction(self.act_quit)
        m_edit = self.menuBar().addMenu("Edit")
        m_edit.addAction(self.act_undo)
        m_edit.addAction(self.act_redo)

        # Toolbar
        toolbar = self.addToolBar("Tools")
        toolbar.setMovable(False)
        toolbar.addAction(self.act_load)
        toolbar.addAction(self.act_quick_save)
        toolbar.addSeparator()
        toolbar.addAction(self.act_undo)
        toolbar.addAction(self.act_redo)
        toolbar.addSeparator()

        group = QtGui.QActionGroup(self)
        for a in (self.act_pan, self.act_probe, None, self.act_erase, self.act_brush, None, self.act_entity_point):
            if a is None:
                toolbar.addSeparator()
            else:
                group.addAction(a)
                toolbar.addAction(a)

        # default tool
        self.act_pan.setChecked(True)
        self.state.set_tool("pan")

    # ---------------- refresh UI from state ----------------

    def refresh_ui_from_state(self):
        self.refresh_lists()
        self.refresh_tool_ui()
        self.refresh_scene_for_selection()
        self.update_selection_label()

    def refresh_tool_ui(self):
        mode = self.state.tool_mode
        self.lbl_tool.setText(f"tool: {mode}")

        # tooling stack index
        if mode == "brush":
            self.right.tool_stack.setCurrentIndex(1)
        elif mode == "probe":
            self.right.tool_stack.setCurrentIndex(2)
        elif mode == "erase":
            self.right.tool_stack.setCurrentIndex(3)
        else:
            self.right.tool_stack.setCurrentIndex(0)

        # set widget values without feedback loops
        self.right.spin_brush.blockSignals(True)
        self.right.spin_probe.blockSignals(True)
        self.right.spin_erase.blockSignals(True)
        self.right.combo_erase_mode.blockSignals(True)

        self.right.spin_brush.setValue(int(self.state.brush_radius))
        self.right.spin_probe.setValue(int(self.state.probe_radius))
        self.right.spin_erase.setValue(int(self.state.erase_radius))

        # erase mode combo selection
        mode_to_idx = {"erase_all": 0, "erase_only_category": 1, "erase_all_but_category": 2}
        self.right.combo_erase_mode.setCurrentIndex(mode_to_idx.get(self.state.erase_mode, 0))

        self.right.spin_brush.blockSignals(False)
        self.right.spin_probe.blockSignals(False)
        self.right.spin_erase.blockSignals(False)
        self.right.combo_erase_mode.blockSignals(False)

        # cursor + drag mode
        if mode == "pan":
            self.act_pan.setChecked(True)
            self.canvas.setDragMode(QtWidgets.QGraphicsView.ScrollHandDrag)
            self.canvas.viewport().setCursor(QtCore.Qt.OpenHandCursor)
        elif mode == "probe":
            self.act_probe.setChecked(True)
            self.canvas.setDragMode(QtWidgets.QGraphicsView.NoDrag)
            self.canvas.viewport().setCursor(QtCore.Qt.WhatsThisCursor)
        elif mode == "brush":
            self.act_brush.setChecked(True)
            self.canvas.setDragMode(QtWidgets.QGraphicsView.NoDrag)
            self.canvas.viewport().setCursor(QtCore.Qt.CrossCursor)
        elif mode == "erase":
            self.act_erase.setChecked(True)
            self.canvas.setDragMode(QtWidgets.QGraphicsView.NoDrag)
            self.canvas.viewport().setCursor(QtCore.Qt.CrossCursor)
        elif mode == "entity_point":
            self.act_entity_point.setChecked(True)
            self.canvas.setDragMode(QtWidgets.QGraphicsView.NoDrag)
            self.canvas.viewport().setCursor(QtCore.Qt.CrossCursor)
        else:
            self.canvas.setDragMode(QtWidgets.QGraphicsView.NoDrag)
            self.canvas.viewport().unsetCursor()

        # ring refresh
        if self._last_mouse_scene_pos:
            self.update_tool_preview_ring(*self._last_mouse_scene_pos)
        else:
            self.preview_ring.setVisible(mode in ("probe", "brush", "erase"))

    def refresh_lists(self):
        p = self.state.project

        # layers
        self.right.layer_list.blockSignals(True)
        self.right.layer_list.clear()
        for l in p.layers:
            item = QtWidgets.QListWidgetItem(l.name)
            item.setData(QtCore.Qt.UserRole, l.id)
            self.right.layer_list.addItem(item)
        self.right.layer_list.blockSignals(False)

        # select current layer
        if self.state.current_layer_id is None and p.layers:
            self.state.current_layer_id = p.layers[0].id

        self._select_list_item_by_id(self.right.layer_list, self.state.current_layer_id)

        # categories
        layer = self.current_layer()
        self.right.category_list.blockSignals(True)
        self.right.category_list.clear()
        if layer:
            for c in layer.categories:
                item = QtWidgets.QListWidgetItem(c.name)
                item.setData(QtCore.Qt.UserRole, c.id)
                qc = rgba_tuple_to_qcolor(c.color)
                item.setForeground(QtGui.QBrush(qc.darker(120)))
                self.right.category_list.addItem(item)
        self.right.category_list.blockSignals(False)

        self._select_list_item_by_id(self.right.category_list, self.state.current_category_id)

        # entities
        self.right.entity_list.blockSignals(True)
        self.right.entity_list.clear()
        if layer:
            for e in layer.entities:
                label = e.name
                if e.category_id:
                    cat = next((c for c in layer.categories if c.id == e.category_id), None)
                    if cat:
                        label = f"{e.name}  [{cat.name}]"
                item = QtWidgets.QListWidgetItem(label)
                item.setData(QtCore.Qt.UserRole, e.id)
                self.right.entity_list.addItem(item)
        self.right.entity_list.blockSignals(False)

        self._select_list_item_by_id(self.right.entity_list, self.state.current_entity_id)

    def refresh_scene_for_selection(self):
        # overlay: show current layer
        self.overlay.show_layer_overlay(self.current_layer())
        # entities: rebuild only for current layer
        self.rebuild_entities()

    def update_selection_label(self):
        layer_name = "-"
        cat_name = "-"
        ent_name = "-"

        layer = self.current_layer()
        if layer:
            layer_name = layer.name
            if self.state.current_category_id:
                cat = next((c for c in layer.categories if c.id == self.state.current_category_id), None)
                if cat:
                    cat_name = cat.name
            if self.state.current_entity_id:
                ent = next((e for e in layer.entities if e.id == self.state.current_entity_id), None)
                if ent:
                    ent_name = ent.name

        self.lbl_sel.setText(f"layer: {layer_name}, category: {cat_name}, entity: {ent_name}")

    @staticmethod
    def _select_list_item_by_id(listw: QtWidgets.QListWidget, obj_id: Optional[str]):
        if not obj_id:
            return
        for i in range(listw.count()):
            if listw.item(i).data(QtCore.Qt.UserRole) == obj_id:
                listw.setCurrentRow(i)
                return

    # ---------------- selection item handlers ----------------

    def _on_layer_item_changed(self, current: Optional[QtWidgets.QListWidgetItem], _prev):
        lid = current.data(QtCore.Qt.UserRole) if current else None
        self.state.set_layer(lid)

    def _on_category_item_changed(self, current: Optional[QtWidgets.QListWidgetItem], _prev):
        cid = current.data(QtCore.Qt.UserRole) if current else None
        self.state.set_category(cid)

    def _on_entity_item_changed(self, current: Optional[QtWidgets.QListWidgetItem], _prev):
        eid = current.data(QtCore.Qt.UserRole) if current else None
        self.state.set_entity(eid)

        # update props editor
        layer = self.current_layer()
        if layer and eid:
            ent = next((e for e in layer.entities if e.id == eid), None)
            if ent:
                self.right.props_editor.setPlainText(json.dumps(ent.props, indent=2, ensure_ascii=False))

    # ---------------- tool parameter handlers ----------------

    def _on_brush_radius_changed(self, v: int):
        self.state.brush_radius = int(v)
        if self._last_mouse_scene_pos:
            self.update_tool_preview_ring(*self._last_mouse_scene_pos)

    def _on_probe_radius_changed(self, v: int):
        self.state.probe_radius = int(v)
        if self._last_mouse_scene_pos:
            self.update_tool_preview_ring(*self._last_mouse_scene_pos)

    def _on_erase_radius_changed(self, v: int):
        self.state.erase_radius = int(v)
        if self._last_mouse_scene_pos:
            self.update_tool_preview_ring(*self._last_mouse_scene_pos)

    def _on_erase_mode_changed(self, _idx: int):
        self.state.erase_mode = self.right.combo_erase_mode.currentData()

    # ---------------- canvas events ----------------

    def on_mouse_moved(self, x: float, y: float):
        self._last_mouse_scene_pos = (x, y)
        self.lbl_pos.setText(f"x: {x:.1f}, y: {y:.1f}")
        self.update_tool_preview_ring(x, y)

    def on_mouse_clicked(self, x: float, y: float):
        p = self.state.project
        if not p.image_path:
            return
        if x < 0 or y < 0 or x >= p.image_width or y >= p.image_height:
            return

        mode = self.state.tool_mode

        if mode in ("brush", "erase"):
            # handled by stroke (continuous)
            return

        if mode == "pan":
            return

        if mode == "probe":
            results, ents = self.editor.probe_at(int(x), int(y))
            lines = [f"Probe @ ({int(x)},{int(y)}) r={self.state.probe_radius}px"]
            if results:
                lines.append("Categories:")
                for pct, name in results[:8]:
                    lines.append(f"  - {name}: {pct:.1f}%")
            else:
                lines.append("Categories: (none)")
            if ents:
                lines.append("Entities:")
                for name in ents[:12]:
                    lines.append(f"  - {name}")
            else:
                lines.append("Entities: (none)")
            QtWidgets.QMessageBox.information(self, "Probe result", "\n".join(lines))
            return

        layer = self.current_layer()

        if not layer:
            return

        if mode == "entity_point":
            if not self.editor.place_entity_point(int(x), int(y)):
                self.status.showMessage("Select an entity, then click to place it.", 2000)
                return
            self.rebuild_entities()
            return

    # ---------------- model helpers ----------------

    def current_layer(self) -> Optional[Layer]:
        for l in self.state.project.layers:
            if l.id == self.state.current_layer_id:
                return l
        return None

    def _ensure_default_layer(self):
        if not self.state.project.layers:
            layer = Layer(id=new_id(), name="Layer 1")
            self.state.project.layers.append(layer)
            self.state.set_layer(layer.id)
        elif self.state.current_layer_id is None:
            self.state.set_layer(self.state.project.layers[0].id)

        self.state.notify_project_changed()

    # ---------------- CRUD: layers/categories/entities ----------------

    def add_layer(self):
        name, ok = QtWidgets.QInputDialog.getText(self, "Add layer", "Layer name:")
        if not ok:
            return
        name = name.strip()
        if not name:
            return
        layer = Layer(id=new_id(), name=name)
        self.state.project.layers.append(layer)
        self.state.set_layer(layer.id)
        self.state.notify_project_changed()

    def edit_layer(self):
        layer = self.current_layer()
        if not layer:
            return
        new_name, ok = QtWidgets.QInputDialog.getText(self, "Edit layer name", "New layer name:", text=layer.name)
        if not ok:
            return
        new_name = new_name.strip()
        if not new_name:
            return
        layer.name = new_name
        self.state.notify_project_changed()

    def delete_layer(self):
        lid = self.state.current_layer_id
        if not lid:
            return
        self.state.project.layers = [l for l in self.state.project.layers if l.id != lid]

        # remove overlay item for that layer (if exists)
        item = self.overlay.layer_overlay_items.get(lid)
        if item is not None:
            self.scene.removeItem(item)
            del self.overlay.layer_overlay_items[lid]

        # select new layer
        new_id_ = self.state.project.layers[0].id if self.state.project.layers else None
        self.state.set_layer(new_id_)
        self.state.notify_project_changed()

    def add_category(self):
        layer = self.current_layer()
        if not layer:
            return

        name, ok = QtWidgets.QInputDialog.getText(self, "Add category", "Category name:")
        if not ok:
            return
        name = name.strip()
        if not name:
            return

        c = QtWidgets.QColorDialog.getColor(QtGui.QColor(50, 200, 50, 140), self, "Category color")
        if not c.isValid():
            c = QtGui.QColor(50, 200, 50, 140)

        used = {cc.index for cc in layer.categories}
        idx = next((i for i in range(1, 256) if i not in used), None)
        if idx is None:
            QtWidgets.QMessageBox.warning(self, "Too many categories", "Max 255 categories per layer.")
            return

        cat = Category(id=new_id(), name=name, color=qcolor_to_rgba_tuple(c), index=idx)
        layer.categories.append(cat)

        self.overlay.invalidate_lut(layer)
        self.state.set_category(cat.id)
        self.state.notify_project_changed()
        # No pixel data changed; overlay does not need full rebuild. But we do need it to display new colors on future paints.

    def rename_category(self):
        layer = self.current_layer()
        cid = self.state.current_category_id
        if not layer or not cid:
            return

        cat = next((c for c in layer.categories if c.id == cid), None)
        if not cat:
            return

        name, ok = QtWidgets.QInputDialog.getText(self, "Rename category", "New Category name:", text=cat.name)
        if not ok:
            return
        name = name.strip()
        if not name:
            return

        cat.name = name
        self.state.notify_project_changed()

    def delete_category(self):
        layer = self.current_layer()
        cid = self.state.current_category_id
        if not layer or not cid:
            return

        deleted_index = self.editor.delete_category_and_clear_pixels(cid)

        # Refresh full overlay because the mask content changed everywhere
        p = self.state.project
        if deleted_index is not None and p.image_path:
            full = QtCore.QRect(0, 0, p.image_width, p.image_height)
            self.overlay.update_layer_overlay(layer, full)

        self.state.set_category(None)
        self.state.notify_project_changed()

    def add_entity(self):
        layer = self.current_layer()
        if not layer:
            return

        name, ok = QtWidgets.QInputDialog.getText(self, "Add entity", "Entity name:")
        if not ok:
            return
        name = name.strip()
        if not name:
            return

        ent = Entity(
            id=new_id(),
            name=name,
            category_id=self.state.current_category_id,
            props={},
            x=self.state.project.image_width / 2 if self.state.project.image_width else 0,
            y=self.state.project.image_height / 2 if self.state.project.image_height else 0,
        )
        layer.entities.append(ent)
        self.state.set_entity(ent.id)
        self.state.notify_project_changed()

        # switch tool for placing
        self.act_entity_point.setChecked(True)
        self.state.set_tool("entity_point")

    def delete_entity(self):
        layer = self.current_layer()
        eid = self.state.current_entity_id
        if not layer or not eid:
            return
        layer.entities = [e for e in layer.entities if e.id != eid]
        self.state.set_entity(None)
        self.state.notify_project_changed()

    def apply_entity_props(self):
        layer = self.current_layer()
        eid = self.state.current_entity_id
        if not layer or not eid:
            return
        ent = next((e for e in layer.entities if e.id == eid), None)
        if not ent:
            return

        text = self.right.props_editor.toPlainText().strip()
        if not text:
            ent.props = {}
            self.state.notify_project_changed()
            return

        try:
            ent.props = json.loads(text)
            self.state.notify_project_changed()
        except Exception as ex:
            QtWidgets.QMessageBox.warning(self, "Invalid JSON", str(ex))

    # ---------------- Stroke Interpolation ----------------

    def on_stroke_started(self, x: float, y: float):
        p = self.state.project
        if not p.image_path:
            return

        ix, iy = int(x), int(y)

        mode = self.state.tool_mode

        # Tools that should NOT continuously draw
        if mode in ("probe", "entity_point", "pan"):
            return

        if not (0 <= ix < p.image_width and 0 <= iy < p.image_height):
            return

        if mode == "brush" and not self.state.current_category_id:
            self.status.showMessage("Select a category to paint.", 1500)
            return

        if mode == "erase" and self.state.erase_mode in ("erase_only_category",
                                                         "erase_all_but_category") and not self.state.current_category_id:
            self.status.showMessage("Select a category for this erase mode.", 1500)
            return

        layer = self.current_layer()
        if not layer:
            return

        self._stroke_rec.begin(layer.id)

        self._stroke_active = True
        self._stroke_last = (ix, iy)

        # First dab immediately
        self._apply_stroke_segment(ix, iy, ix, iy)

    def on_stroke_moved(self, x: float, y: float):
        if not self._stroke_active:
            return

        p = self.state.project
        ix, iy = int(x), int(y)

        if not (0 <= ix < p.image_width and 0 <= iy < p.image_height):
            return

        lx, ly = self._stroke_last
        if ix == lx and iy == ly:
            return

        self._apply_stroke_segment(lx, ly, ix, iy)
        self._stroke_last = (ix, iy)

    def on_stroke_ended(self, x: float, y: float):
        # finalize undo record (if any)
        if self._stroke_rec.is_active():
            p = self.state.project
            layer = self.current_layer()
            if layer and layer.id == self._stroke_rec.layer_id and p.image_path:
                mask = self.mask_store.ensure_layer_index_mask(layer)
                cmd = self._stroke_rec.build_command(
                    mask=mask,
                    get_layer_fn=self._get_layer_by_id,
                    overlay=self.overlay,
                    img_w=p.image_width,
                    img_h=p.image_height,
                )
                if cmd is not None:
                    self.undo_stack.push(cmd)

            # stop recording
            self._stroke_rec.layer_id = None

        self._stroke_active = False
        self._stroke_last = None

    def _get_layer_by_id(self, layer_id: str) -> Optional[Layer]:
        for l in self.state.project.layers:
            if l.id == layer_id:
                return l
        return None

    def _apply_stroke_segment(self, x0: int, y0: int, x1: int, y1: int):
        p = self.state.project
        layer = self.current_layer()
        if not layer or not p.image_path:
            return

        mode = self.state.tool_mode

        # Determine tool radius
        if mode == "brush":
            r = int(self.state.brush_radius)
        elif mode == "erase":
            r = int(self.state.erase_radius)
        else:
            return

        if r <= 0:
            return

        # Step size controls stroke density
        step = max(1.0, r * 0.5)

        dx = x1 - x0
        dy = y1 - y0
        dist = (dx * dx + dy * dy) ** 0.5

        if dist == 0:
            points = [(x0, y0)]
        else:
            n = max(1, int(dist / step))
            points = []
            for i in range(n + 1):
                t = i / n
                px = int(round(x0 + dx * t))
                py = int(round(y0 + dy * t))
                points.append((px, py))

        # Get mask bytes once; apply many dabs
        mask = self.mask_store.ensure_layer_index_mask(layer)
        mbytes = self.overlay.qimage_bytes(mask)  # OverlayRenderer.qimage_bytes
        bpl = mask.bytesPerLine()

        dirty_union = None

        # If recording, capture "before" for the tiles this segment could touch
        if self._stroke_rec.is_active() and self._stroke_rec.layer_id == layer.id:
            seg_rect = QtCore.QRect(
                min(x0, x1) - r,
                min(y0, y1) - r,
                abs(x1 - x0) + 2 * r + 1,
                abs(y1 - y0) + 2 * r + 1,
            )
            self._stroke_rec.capture_before_for_rect(mask, seg_rect, p.image_width, p.image_height)

        if mode == "brush":
            cat = next((c for c in layer.categories if c.id == self.state.current_category_id), None)
            if not cat:
                return
            idx = int(cat.index)

            for (px, py) in points:
                dr = self._dab_set_index(mbytes, bpl, px, py, r, idx)
                dirty_union = dr if dirty_union is None else dirty_union.united(dr)

        elif mode == "erase":
            erase_mode = self.state.erase_mode

            selected_idx = None
            if erase_mode in ("erase_only_category", "erase_all_but_category"):
                cat = next((c for c in layer.categories if c.id == self.state.current_category_id), None)
                if not cat:
                    return
                selected_idx = int(cat.index)

            for (px, py) in points:
                dr = self._dab_erase(mbytes, bpl, px, py, r, erase_mode, selected_idx)
                dirty_union = dr if dirty_union is None else dirty_union.united(dr)

        if dirty_union is None:
            return

        dirty_union = clamp_rect_to_image(dirty_union, p.image_width, p.image_height)

        # Update overlay pixels once for the union rect
        self.overlay.update_layer_overlay(layer, dirty_union)

    def _dab_set_index(self, mbytes: memoryview, bpl: int, x: int, y: int, r: int, idx: int) -> QtCore.QRect:
        p = self.state.project
        w, h = p.image_width, p.image_height

        x0 = max(0, x - r)
        x1 = min(w - 1, x + r)
        y0 = max(0, y - r)
        y1 = min(h - 1, y + r)

        rr = r * r
        for yy in range(y0, y1 + 1):
            dy = yy - y
            row = yy * bpl
            for xx in range(x0, x1 + 1):
                dx = xx - x
                if dx * dx + dy * dy <= rr:
                    mbytes[row + xx] = idx

        return QtCore.QRect(x0, y0, (x1 - x0 + 1), (y1 - y0 + 1))

    def _dab_erase(
            self,
            mbytes: memoryview,
            bpl: int,
            x: int,
            y: int,
            r: int,
            mode: str,
            selected_idx: int | None,
    ) -> QtCore.QRect:
        p = self.state.project
        w, h = p.image_width, p.image_height

        x0 = max(0, x - r)
        x1 = min(w - 1, x + r)
        y0 = max(0, y - r)
        y1 = min(h - 1, y + r)

        rr = r * r
        for yy in range(y0, y1 + 1):
            dy = yy - y
            row = yy * bpl
            for xx in range(x0, x1 + 1):
                dx = xx - x
                if dx * dx + dy * dy > rr:
                    continue

                pos = row + xx
                cur = int(mbytes[pos])

                if mode == "erase_all":
                    if cur != 0:
                        mbytes[pos] = 0
                elif mode == "erase_only_category":
                    if selected_idx is not None and cur == selected_idx:
                        mbytes[pos] = 0
                elif mode == "erase_all_but_category":
                    if selected_idx is not None and cur != 0 and cur != selected_idx:
                        mbytes[pos] = 0

        return QtCore.QRect(x0, y0, (x1 - x0 + 1), (y1 - y0 + 1))

    # ---------------- entity rendering ----------------

    def rebuild_entities(self):
        # Remove old
        if not hasattr(self, "_entity_items"):
            self._entity_items: Dict[str, QtWidgets.QGraphicsItem] = {}
        for _eid, item in list(self._entity_items.items()):
            self.scene.removeItem(item)
        self._entity_items.clear()

        layer = self.current_layer()
        p = self.state.project
        if not layer or not p.image_path:
            return

        for e in layer.entities:
            group = QtWidgets.QGraphicsItemGroup()
            r = 5
            circle = QtWidgets.QGraphicsEllipseItem(e.x - r, e.y - r, r * 2, r * 2)
            circle.setBrush(QtGui.QBrush(QtGui.QColor(255, 255, 255, 220)))
            circle.setPen(QtGui.QPen(QtGui.QColor(0, 0, 0, 180), 1))
            circle.setZValue(100)

            label = QtWidgets.QGraphicsSimpleTextItem(e.name)
            label.setBrush(QtGui.QBrush(QtGui.QColor(0, 0, 0, 200)))
            label.setPos(e.x + 8, e.y - 10)
            label.setZValue(101)

            group.addToGroup(circle)
            group.addToGroup(label)
            group.setZValue(100)
            self.scene.addItem(group)
            self._entity_items[e.id] = group

    # ---------------- import / scene ----------------

    def import_image(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Import image", "", "Images (*.png *.jpg *.jpeg *.bmp *.tif *.tiff)"
        )
        if not path:
            return

        img = QtGui.QImage(path)
        if img.isNull():
            QtWidgets.QMessageBox.warning(self, "Import failed", "Could not read image.")
            return

        self.state.project.image_path = path
        self.state.project.image_width = img.width()
        self.state.project.image_height = img.height()

        self.scene.clear()
        self.ensure_preview_ring()
        self.overlay.clear_overlay_items()
        if hasattr(self, "_entity_items"):
            self._entity_items.clear()

        self.base_pixmap_item = self.scene.addPixmap(QtGui.QPixmap.fromImage(img))
        self.base_pixmap_item.setZValue(0)

        self.scene.setSceneRect(0, 0, img.width(), img.height())
        self.canvas.resetTransform()
        self.canvas.fitInView(self.scene.sceneRect(), QtCore.Qt.KeepAspectRatio)

        # Ensure each layer has a runtime index mask
        for layer in self.state.project.layers:
            self.mask_store.ensure_layer_index_mask(layer)

        self.state.notify_project_changed()

    # ---------------- save / load ----------------

    def persist_last_project_path(self, path: str):
        self.settings.setValue("last_project_json", path)

    def load_last_project_on_startup(self):
        path = self.settings.value("last_project_json", "", type=str)
        if not path:
            return
        if not os.path.exists(path):
            self.settings.remove("last_project_json")
            return
        self.load_project_json(path)

    def quick_save_project_json(self):
        path = self.settings.value("last_project_json", "", type=str)
        self.save_project_json(path)

    def save_project_json(self, path: Optional[str] = None):
        if not path:
            path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save project JSON", "", "JSON (*.json)")
        if not path:
            return

        # encode masks at save time
        self.mask_store.encode_all_masks_to_b64()
        ProjectCodec.save_json(path, self.state.project)

        self.persist_last_project_path(path)
        self.status.showMessage(f"Saved: {path}", 2500)

    def load_project_json(self, path: Optional[str] = None):
        if not path:
            path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Load project JSON", "", "JSON (*.json)")
        if not path:
            return

        try:
            project = ProjectCodec.load_json(path)
            self.persist_last_project_path(path)
        except Exception as ex:
            QtWidgets.QMessageBox.warning(self, "Load failed", str(ex))
            return

        self.state.project = project

        # Ensure at least one layer
        if not self.state.project.layers:
            self.state.project.layers.append(Layer(id=new_id(), name="Layer 1"))

        # pick first layer
        self.state.current_layer_id = self.state.project.layers[0].id
        self.state.current_category_id = None
        self.state.current_entity_id = None

        # reload base image
        if self.state.project.image_path and os.path.exists(self.state.project.image_path):
            img = QtGui.QImage(self.state.project.image_path)
            if not img.isNull():
                self.scene.clear()
                self.ensure_preview_ring()
                self.overlay.clear_overlay_items()
                if hasattr(self, "_entity_items"):
                    self._entity_items.clear()

                self.base_pixmap_item = self.scene.addPixmap(QtGui.QPixmap.fromImage(img))
                self.base_pixmap_item.setZValue(0)
                self.scene.setSceneRect(0, 0, img.width(), img.height())
                self.canvas.resetTransform()
                self.canvas.fitInView(self.scene.sceneRect(), QtCore.Qt.KeepAspectRatio)
        else:
            self.scene.clear()
            self.ensure_preview_ring()
            self.overlay.clear_overlay_items()
            self.base_pixmap_item = None

        # preload masks for correctness
        if self.state.project.image_path:
            for layer in self.state.project.layers:
                self.mask_store.ensure_layer_index_mask(layer)

        self.state.notify_project_changed()
        self.status.showMessage(f"Loaded: {path}", 2500)

    # ---------------- misc ----------------

    def confirm_quit(self):
        res = QtWidgets.QMessageBox.question(
            self,
            "Quit PolyPixTagger",
            "Really Quit?\nYou may lose unsaved changes!",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
            QtWidgets.QMessageBox.No,
        )
        if res == QtWidgets.QMessageBox.Yes:
            QtWidgets.QApplication.quit()


def main():
    app = QtWidgets.QApplication([])
    w = PixTagMainWindow()
    w.show()
    app.exec()


if __name__ == "__main__":
    main()
