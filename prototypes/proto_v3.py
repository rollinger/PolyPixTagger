import base64
import io
import json
import os
import uuid
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple

from PySide6 import QtCore, QtGui, QtWidgets


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


@dataclass
class Category:
    id: str
    name: str
    color: Tuple[int, int, int, int]  # RGBA


@dataclass
class Entity:
    id: str
    name: str
    category_id: Optional[str] = None
    props: dict = field(default_factory=dict)
    # prototype shape: a point in image pixel coords
    x: float = 0.0
    y: float = 0.0


@dataclass
class Layer:
    id: str
    name: str
    categories: List[Category] = field(default_factory=list)
    entities: List[Entity] = field(default_factory=list)
    # category_id -> mask (stored as base64 png in JSON)
    category_masks: Dict[str, Optional[str]] = field(default_factory=dict)


@dataclass
class Project:
    image_path: Optional[str] = None
    image_width: int = 0
    image_height: int = 0
    layers: List[Layer] = field(default_factory=list)


class ImageCanvas(QtWidgets.QGraphicsView):
    mouseMoved = QtCore.Signal(float, float)  # scene coords
    mouseClicked = QtCore.Signal(float, float)  # scene coords

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

    def wheelEvent(self, event: QtGui.QWheelEvent):
        if event.modifiers() & QtCore.Qt.ControlModifier:
            delta = event.angleDelta().y()
            factor = 1.25 if delta > 0 else 0.8
            self.scale(factor, factor)
            event.accept()
            return
        super().wheelEvent(event)

    def mouseMoveEvent(self, event: QtGui.QMouseEvent):
        p = self.mapToScene(event.position().toPoint())
        self.mouseMoved.emit(p.x(), p.y())
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event: QtGui.QMouseEvent):
        if event.button() == QtCore.Qt.LeftButton:
            p = self.mapToScene(event.position().toPoint())
            self.mouseClicked.emit(p.x(), p.y())
        if event.button() == QtCore.Qt.LeftButton and self.dragMode() == QtWidgets.QGraphicsView.ScrollHandDrag:
            self.viewport().setCursor(QtCore.Qt.ClosedHandCursor)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QtGui.QMouseEvent):
        if self.dragMode() == QtWidgets.QGraphicsView.ScrollHandDrag:
            self.viewport().setCursor(QtCore.Qt.OpenHandCursor)
        super().mouseReleaseEvent(event)

class PixTagMainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QtCore.QSettings("PolyPixTagger", "PolyPixTagger")
        self.setWindowTitle("PixTag Prototype (PySide6 / Qt)")
        self.resize(1400, 900)

        self.project = Project()

        # Tool state
        self.tool_mode = "brush"  # brush | probe | entity_point
        self.brush_radius = 6
        self.probe_radius = 6
        self.erase_radius = 6
        self.erase_mode = "erase_all"  # erase_all | erase_only_category | erase_all_but_category

        # Selection state
        self.current_layer_id: Optional[str] = None
        self.current_category_id: Optional[str] = None
        self.current_entity_id: Optional[str] = None

        # Scene
        self.scene = QtWidgets.QGraphicsScene(self)

        # Tool preview ring (scene overlay, zoom-aware)
        self.preview_ring = None
        self.ensure_preview_ring()
        self._last_mouse_scene_pos = None

        self.canvas = ImageCanvas()
        self.canvas.setScene(self.scene)
        self.canvas.mouseMoved.connect(self.on_mouse_moved)
        self.canvas.mouseClicked.connect(self.on_mouse_clicked)

        self.base_pixmap_item: Optional[QtWidgets.QGraphicsPixmapItem] = None
        # category overlay pixmap items: (layer_id, category_id) -> item
        self.overlay_items: Dict[Tuple[str, str], QtWidgets.QGraphicsPixmapItem] = {}
        # entity graphics items: entity_id -> item
        self.entity_items: Dict[str, QtWidgets.QGraphicsItem] = {}

        # Right panel UI
        self.layer_list = QtWidgets.QListWidget()
        self.category_list = QtWidgets.QListWidget()
        self.entity_list = QtWidgets.QListWidget()
        self.props_editor = QtWidgets.QPlainTextEdit()
        self.props_editor.setPlaceholderText('Entity properties JSON (e.g. {"type":"forest","owner":"..."} )')

        self.layer_list.currentItemChanged.connect(self.on_layer_selected)
        self.category_list.currentItemChanged.connect(self.on_category_selected)
        self.entity_list.currentItemChanged.connect(self.on_entity_selected)

        # Buttons on right panel
        self.btn_add_layer = QtWidgets.QPushButton("Add layer")
        self.btn_del_layer = QtWidgets.QPushButton("Delete layer")
        self.btn_add_cat = QtWidgets.QPushButton("Add category")
        self.btn_del_cat = QtWidgets.QPushButton("Delete category")
        self.btn_add_ent = QtWidgets.QPushButton("Add entity (point)")
        self.btn_del_ent = QtWidgets.QPushButton("Delete entity")
        self.btn_apply_props = QtWidgets.QPushButton("Apply entity JSON")
        self.btn_add_layer.clicked.connect(self.add_layer)
        self.btn_del_layer.clicked.connect(self.delete_layer)
        self.btn_add_cat.clicked.connect(self.add_category)
        self.btn_del_cat.clicked.connect(self.delete_category)
        self.btn_add_ent.clicked.connect(self.add_entity)
        self.btn_del_ent.clicked.connect(self.delete_entity)
        self.btn_apply_props.clicked.connect(self.apply_entity_props)

        # Right panel layout
        right = QtWidgets.QWidget()
        right_layout = QtWidgets.QVBoxLayout(right)
        right_layout.setContentsMargins(8, 8, 8, 8)

        # --- Tooling pane (RIGHT, top) ---
        self.tool_stack = QtWidgets.QStackedWidget()

        # Brush page
        brush_page = QtWidgets.QWidget()
        brush_layout = QtWidgets.QFormLayout(brush_page)
        brush_layout.setContentsMargins(0, 0, 0, 0)

        self.spin_brush = QtWidgets.QSpinBox()
        self.spin_brush.setRange(1, 100)
        self.spin_brush.setValue(self.brush_radius)
        self.spin_brush.valueChanged.connect(self.on_brush_radius_changed)
        brush_layout.addRow("Brush radius (px)", self.spin_brush)

        # Probe page
        probe_page = QtWidgets.QWidget()
        probe_layout = QtWidgets.QFormLayout(probe_page)
        probe_layout.setContentsMargins(0, 0, 0, 0)

        self.spin_probe = QtWidgets.QSpinBox()
        self.spin_probe.setRange(1, 100)
        self.spin_probe.setValue(self.probe_radius)
        self.spin_probe.valueChanged.connect(self.on_probe_radius_changed)
        probe_layout.addRow("Probe radius (px)", self.spin_probe)

        # Erase page
        erase_page = QtWidgets.QWidget()
        erase_layout = QtWidgets.QFormLayout(erase_page)
        erase_layout.setContentsMargins(0, 0, 0, 0)

        self.spin_erase = QtWidgets.QSpinBox()
        self.spin_erase.setRange(1, 100)
        self.spin_erase.setValue(self.erase_radius)
        self.spin_erase.valueChanged.connect(self.on_erase_radius_changed)
        erase_layout.addRow("Eraser radius (px)", self.spin_erase)

        self.combo_erase_mode = QtWidgets.QComboBox()
        self.combo_erase_mode.addItem("Erase all", "erase_all")
        self.combo_erase_mode.addItem("Erase only category", "erase_only_category")
        self.combo_erase_mode.addItem("Erase all but category", "erase_all_but_category")
        self.combo_erase_mode.currentIndexChanged.connect(self.on_erase_mode_changed)
        erase_layout.addRow("Mode", self.combo_erase_mode)

        # Optional page for tools without params (Pan/Entity) - can be empty or a label
        empty_page = QtWidgets.QWidget()
        empty_layout = QtWidgets.QVBoxLayout(empty_page)
        empty_layout.setContentsMargins(0, 0, 0, 0)
        empty_layout.addWidget(QtWidgets.QLabel("No parameters for this tool."))

        # Add pages in a known order
        self.tool_stack.addWidget(empty_page)  # index 0
        self.tool_stack.addWidget(brush_page)  # index 1
        self.tool_stack.addWidget(probe_page)  # index 2
        self.tool_stack.addWidget(erase_page)  # index 3

        tool_group = QtWidgets.QGroupBox("Tooling")
        tool_group_layout = QtWidgets.QVBoxLayout(tool_group)
        tool_group_layout.addWidget(self.tool_stack)

        # Put tooling pane ABOVE Layers
        right_layout.addWidget(tool_group)
        right_layout.addSpacing(6)

        # --- Layer pane (RIGHT) ---
        right_layout.addWidget(QtWidgets.QLabel("Layers"))
        right_layout.addWidget(self.layer_list, 1)
        rowL = QtWidgets.QHBoxLayout()
        rowL.addWidget(self.btn_add_layer)
        rowL.addWidget(self.btn_del_layer)
        right_layout.addLayout(rowL)
        right_layout.addSpacing(6)

        # --- Categories pane (RIGHT) ---
        right_layout.addWidget(QtWidgets.QLabel("Categories (per layer)"))
        right_layout.addWidget(self.category_list, 1)
        rowC = QtWidgets.QHBoxLayout()
        rowC.addWidget(self.btn_add_cat)
        rowC.addWidget(self.btn_del_cat)
        right_layout.addLayout(rowC)
        right_layout.addSpacing(6)

        # --- Entities pane (RIGHT) ---
        right_layout.addWidget(QtWidgets.QLabel("Entities (per layer)"))
        right_layout.addWidget(self.entity_list, 1)
        rowE = QtWidgets.QHBoxLayout()
        rowE.addWidget(self.btn_add_ent)
        rowE.addWidget(self.btn_del_ent)
        right_layout.addLayout(rowE)
        right_layout.addSpacing(6)

        # --- Entity Properties pane (RIGHT) ---
        right_layout.addWidget(QtWidgets.QLabel("Selected entity properties (JSON)"))
        right_layout.addWidget(self.props_editor, 2)
        right_layout.addWidget(self.btn_apply_props)

        # Splitter main
        splitter = QtWidgets.QSplitter()
        splitter.addWidget(self.canvas)
        splitter.addWidget(right)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([800, 200])

        self.setCentralWidget(splitter)

        # Status bar bottom row
        self.status = self.statusBar()
        self.lbl_pos = QtWidgets.QLabel("x: -, y: -")
        self.lbl_tool = QtWidgets.QLabel("tool: brush")
        self.lbl_sel = QtWidgets.QLabel("layer: -, category: -, entity: -")
        self.status.addPermanentWidget(self.lbl_pos)
        self.status.addPermanentWidget(self.lbl_tool)
        self.status.addPermanentWidget(self.lbl_sel)

        # Menu + toolbar
        self._build_actions()

        # Start with last project if present
        QtCore.QTimer.singleShot(0, self.load_last_project_on_startup)

    def ensure_preview_ring(self):
        # If it doesn't exist OR Qt has deleted the C++ object, recreate it
        if getattr(self, "preview_ring", None) is not None:
            try:
                self.preview_ring.isVisible()  # any call that touches C++ object
                return
            except RuntimeError:
                pass  # deleted

        self.preview_ring = QtWidgets.QGraphicsEllipseItem()
        pen = QtGui.QPen(QtGui.QColor(255, 255, 255, 220), 1)
        pen.setCosmetic(True)
        self.preview_ring.setPen(pen)
        self.preview_ring.setBrush(QtCore.Qt.NoBrush)
        self.preview_ring.setZValue(10_000)
        self.preview_ring.setVisible(False)
        self.scene.addItem(self.preview_ring)

    # ---------- UI Actions ----------
    def _build_actions(self):
        # Open new image/project
        act_new = QtGui.QAction(
            self.style().standardIcon(QtWidgets.QStyle.SP_FileDialogNewFolder),
            "New Image/Project",
            self,
        )
        act_new.triggered.connect(self.import_image)
        # Save project JSON
        act_save = QtGui.QAction(
            self.style().standardIcon(QtWidgets.QStyle.SP_DialogSaveButton),
            "Save project JSON",
            self,
        )
        act_save.triggered.connect(self.save_project_json)
        # Load project JSON
        act_load = QtGui.QAction(
            self.style().standardIcon(QtWidgets.QStyle.SP_DialogOpenButton),
            "Load project JSON",
            self,
        )
        act_load.triggered.connect(self.load_project_json)
        # Quit application
        act_quit = QtGui.QAction(
            self.style().standardIcon(QtWidgets.QStyle.SP_DialogCloseButton),
            "Quit",
            self,
        )
        act_quit.setShortcut(QtGui.QKeySequence.Quit) # Ctrl+Q, Cmd+Q
        act_quit.triggered.connect(self.confirm_quit)

        menu = self.menuBar()
        m_file = menu.addMenu("File")
        m_file.addAction(act_new)
        m_file.addAction(act_load)
        m_file.addAction(act_save)
        m_file.addSeparator()
        m_file.addAction(act_quit)

        toolbar = self.addToolBar("Tools")
        toolbar.setMovable(False)
        toolbar.addAction(act_load)
        toolbar.addAction(act_save)
        toolbar.addSeparator()

        # -- General Tools --
        self.act_pan = QtGui.QAction("Pan", self, checkable=True)
        self.act_pan.setShortcut(QtGui.QKeySequence("Ctrl+M"))
        self.act_pan.triggered.connect(lambda: self.set_tool("pan"))

        self.act_probe = QtGui.QAction("Probe", self, checkable=True)
        self.act_probe.setShortcut(QtGui.QKeySequence("Ctrl+P"))
        self.act_probe.triggered.connect(lambda: self.set_tool("probe"))

        # -- Pixel Tools --
        self.act_erase = QtGui.QAction("Erase", self, checkable=True)
        self.act_erase.setShortcut(QtGui.QKeySequence("Ctrl+E"))
        self.act_erase.setShortcutContext(QtCore.Qt.ApplicationShortcut)
        self.act_erase.triggered.connect(lambda: self.set_tool("erase"))

        self.act_brush = QtGui.QAction("Brush", self, checkable=True)
        self.act_brush.setShortcut(QtGui.QKeySequence("Ctrl+B"))
        self.act_brush.triggered.connect(lambda: self.set_tool("brush"))

        self.act_spray = QtGui.QAction("Spray", self, checkable=True)
        self.act_spray.setShortcut(QtGui.QKeySequence("Ctrl+S"))
        self.act_spray.triggered.connect(lambda: self.set_tool("spray"))

        group = QtGui.QActionGroup(self)
        for a in (self.act_pan, self.act_probe, "|", self.act_erase, self.act_brush, self.act_spray):
            if isinstance(a, str):
                if a == "|":
                    toolbar.addSeparator()
            else:
                group.addAction(a)
                toolbar.addAction(a)

        # Default to pan tool
        self.act_pan.setChecked(True)
        self.set_tool("pan")

        # -- Pixel Tools --
        #self.act_entity = QtGui.QAction("Entity point", self, checkable=True)
        #self.act_entity.triggered.connect(lambda: self.set_tool("entity_point"))


    def set_tool(self, mode: str):
        self.tool_mode = mode
        self.lbl_tool.setText(f"tool: {mode}")

        # update tooling preview immediately (show/hide ring)
        if self._last_mouse_scene_pos:
            x, y = self._last_mouse_scene_pos
            self.update_tool_preview_ring(x, y)
        else:
            self.preview_ring.setVisible(mode in ("probe", "brush", "erase"))

        # Keep toolbar toggle state consistent even when tool is set via ESC
        # Change Mouse Cursor to the tool's cursor'
        if mode == "pan":
            self.act_pan.setChecked(True)
            self.canvas.setDragMode(QtWidgets.QGraphicsView.ScrollHandDrag)
            self.canvas.viewport().setCursor(QtCore.Qt.OpenHandCursor)
            self.tool_stack.setCurrentIndex(0)
        elif mode == "probe":
            self.act_probe.setChecked(True)
            self.canvas.setDragMode(QtWidgets.QGraphicsView.NoDrag)
            self.canvas.viewport().setCursor(QtCore.Qt.WhatsThisCursor)
            self.tool_stack.setCurrentIndex(2)
        elif mode == "brush":
            self.act_brush.setChecked(True)
            self.canvas.setDragMode(QtWidgets.QGraphicsView.NoDrag)
            self.canvas.viewport().setCursor(QtCore.Qt.CrossCursor)
            self.tool_stack.setCurrentIndex(1)
        elif mode == "erase":
            self.act_erase.setChecked(True)
            self.canvas.setDragMode(QtWidgets.QGraphicsView.NoDrag)
            self.canvas.viewport().setCursor(QtCore.Qt.CrossCursor)
            self.tool_stack.setCurrentIndex(3)
        elif mode == "spray":
            self.act_spray.setChecked(True)
            self.canvas.setDragMode(QtWidgets.QGraphicsView.NoDrag)
            self.canvas.viewport().setCursor(QtCore.Qt.CrossCursor)
            self.tool_stack.setCurrentIndex(0)
        else:
            self.canvas.setDragMode(QtWidgets.QGraphicsView.NoDrag)
            self.canvas.viewport().unsetCursor()
            self.tool_stack.setCurrentIndex(0)

    # ---------- Import / Scene ----------
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

        self.project.image_path = path
        self.project.image_width = img.width()
        self.project.image_height = img.height()

        self.scene.clear()
        self.ensure_preview_ring()
        self.overlay_items.clear()
        self.entity_items.clear()

        self.base_pixmap_item = self.scene.addPixmap(QtGui.QPixmap.fromImage(img))
        self.base_pixmap_item.setZValue(0)

        self.scene.setSceneRect(0, 0, img.width(), img.height())
        self.canvas.resetTransform()
        self.canvas.fitInView(self.scene.sceneRect(), QtCore.Qt.KeepAspectRatio)

        # ensure existing layers have masks initialized
        for layer in self.project.layers:
            for cat in layer.categories:
                if cat.id not in layer.category_masks:
                    layer.category_masks[cat.id] = None
        self.rebuild_overlays()
        self.rebuild_entities()

    # ---------- Layers / Categories / Entities ----------
    def current_layer(self) -> Optional[Layer]:
        for l in self.project.layers:
            if l.id == self.current_layer_id:
                return l
        return None

    def add_layer(self, name: Optional[str] = None):
        if name is None:
            name, ok = QtWidgets.QInputDialog.getText(self, "Add layer", "Layer name:")
            if not ok or not name.strip():
                return
            name = name.strip()

        layer = Layer(id=new_id(), name=name)
        self.project.layers.append(layer)
        self.refresh_layer_list(select_id=layer.id)

    def delete_layer(self):
        if not self.current_layer_id:
            return
        self.project.layers = [l for l in self.project.layers if l.id != self.current_layer_id]
        self.current_layer_id = None
        self.refresh_layer_list()
        self.refresh_category_list()
        self.refresh_entity_list()
        self.rebuild_overlays()
        self.rebuild_entities()
        self.update_selection_label()

    def add_category(self):
        layer = self.current_layer()
        if not layer:
            return

        name, ok = QtWidgets.QInputDialog.getText(self, "Add category", "Category name:")
        if not ok or not name.strip():
            return
        name = name.strip()

        # pick a color
        c = QtWidgets.QColorDialog.getColor(QtGui.QColor(50, 200, 50, 140), self, "Category color")
        if not c.isValid():
            c = QtGui.QColor(50, 200, 50, 140)

        cat = Category(id=new_id(), name=name, color=qcolor_to_rgba_tuple(c))
        layer.categories.append(cat)
        layer.category_masks.setdefault(cat.id, None)

        self.refresh_category_list(select_id=cat.id)
        self.rebuild_overlays()

    def delete_category(self):
        layer = self.current_layer()
        if not layer or not self.current_category_id:
            return

        cid = self.current_category_id
        layer.categories = [c for c in layer.categories if c.id != cid]
        if cid in layer.category_masks:
            del layer.category_masks[cid]

        # also detach entities from that category
        for e in layer.entities:
            if e.category_id == cid:
                e.category_id = None

        self.current_category_id = None
        self.refresh_category_list()
        self.refresh_entity_list()
        self.rebuild_overlays()
        self.update_selection_label()

    def add_entity(self):
        layer = self.current_layer()
        if not layer:
            return

        name, ok = QtWidgets.QInputDialog.getText(self, "Add entity", "Entity name:")
        if not ok or not name.strip():
            return
        name = name.strip()

        ent = Entity(
            id=new_id(),
            name=name,
            category_id=self.current_category_id,
            props={},
            x=self.project.image_width / 2 if self.project.image_width else 0,
            y=self.project.image_height / 2 if self.project.image_height else 0,
        )
        layer.entities.append(ent)
        self.refresh_entity_list(select_id=ent.id)
        self.rebuild_entities()

        # switch tool to place entity point quickly (optional)
        self.set_tool("entity_point")
        self.act_entity.setChecked(True)

    def delete_entity(self):
        layer = self.current_layer()
        if not layer or not self.current_entity_id:
            return
        eid = self.current_entity_id
        layer.entities = [e for e in layer.entities if e.id != eid]
        self.current_entity_id = None
        self.refresh_entity_list()
        self.rebuild_entities()
        self.update_selection_label()

    def apply_entity_props(self):
        layer = self.current_layer()
        if not layer or not self.current_entity_id:
            return
        ent = next((e for e in layer.entities if e.id == self.current_entity_id), None)
        if not ent:
            return
        text = self.props_editor.toPlainText().strip()
        if not text:
            ent.props = {}
            return
        try:
            ent.props = json.loads(text)
        except Exception as ex:
            QtWidgets.QMessageBox.warning(self, "Invalid JSON", str(ex))

    # ---------- List refresh ----------
    def refresh_layer_list(self, select_id: Optional[str] = None):
        self.layer_list.blockSignals(True)
        self.layer_list.clear()
        for l in self.project.layers:
            item = QtWidgets.QListWidgetItem(l.name)
            item.setData(QtCore.Qt.UserRole, l.id)
            self.layer_list.addItem(item)
        self.layer_list.blockSignals(False)

        # select
        if select_id is None and self.project.layers:
            select_id = self.project.layers[0].id

        if select_id:
            for i in range(self.layer_list.count()):
                if self.layer_list.item(i).data(QtCore.Qt.UserRole) == select_id:
                    self.layer_list.setCurrentRow(i)
                    break

    def refresh_category_list(self, select_id: Optional[str] = None):
        self.category_list.blockSignals(True)
        self.category_list.clear()
        layer = self.current_layer()
        if layer:
            for c in layer.categories:
                item = QtWidgets.QListWidgetItem(c.name)
                item.setData(QtCore.Qt.UserRole, c.id)
                qc = rgba_tuple_to_qcolor(c.color)
                item.setForeground(QtGui.QBrush(qc.darker(120)))
                self.category_list.addItem(item)
        self.category_list.blockSignals(False)

        if select_id:
            for i in range(self.category_list.count()):
                if self.category_list.item(i).data(QtCore.Qt.UserRole) == select_id:
                    self.category_list.setCurrentRow(i)
                    break

    def refresh_entity_list(self, select_id: Optional[str] = None):
        self.entity_list.blockSignals(True)
        self.entity_list.clear()
        layer = self.current_layer()
        if layer:
            for e in layer.entities:
                label = e.name
                if e.category_id:
                    cat = next((c for c in layer.categories if c.id == e.category_id), None)
                    if cat:
                        label = f"{e.name}  [{cat.name}]"
                item = QtWidgets.QListWidgetItem(label)
                item.setData(QtCore.Qt.UserRole, e.id)
                self.entity_list.addItem(item)
        self.entity_list.blockSignals(False)

        if select_id:
            for i in range(self.entity_list.count()):
                if self.entity_list.item(i).data(QtCore.Qt.UserRole) == select_id:
                    self.entity_list.setCurrentRow(i)
                    break

    # ---------- Selection handlers ----------
    def on_layer_selected(self, current: Optional[QtWidgets.QListWidgetItem], prev):
        self.current_layer_id = current.data(QtCore.Qt.UserRole) if current else None
        self.current_category_id = None
        self.current_entity_id = None
        self.refresh_category_list()
        self.refresh_entity_list()
        self.rebuild_overlays()
        self.rebuild_entities()
        self.update_selection_label()

    def on_category_selected(self, current: Optional[QtWidgets.QListWidgetItem], prev):
        self.current_category_id = current.data(QtCore.Qt.UserRole) if current else None
        self.update_selection_label()

    def on_entity_selected(self, current: Optional[QtWidgets.QListWidgetItem], prev):
        self.current_entity_id = current.data(QtCore.Qt.UserRole) if current else None
        layer = self.current_layer()
        if layer and self.current_entity_id:
            ent = next((e for e in layer.entities if e.id == self.current_entity_id), None)
            if ent:
                self.props_editor.setPlainText(json.dumps(ent.props, indent=2, ensure_ascii=False))
        self.update_selection_label()

    def update_selection_label(self):
        layer_name = "-"
        cat_name = "-"
        ent_name = "-"
        layer = self.current_layer()
        if layer:
            layer_name = layer.name
            if self.current_category_id:
                cat = next((c for c in layer.categories if c.id == self.current_category_id), None)
                if cat:
                    cat_name = cat.name
            if self.current_entity_id:
                ent = next((e for e in layer.entities if e.id == self.current_entity_id), None)
                if ent:
                    ent_name = ent.name
        self.lbl_sel.setText(f"layer: {layer_name}, category: {cat_name}, entity: {ent_name}")

    # ---------- Painting + Probing ----------
    def update_tool_preview_ring(self, x: float, y: float):
        self.ensure_preview_ring()
        # only show for tools that operate with a radius
        if self.tool_mode == "brush":
            r = self.brush_radius
        elif self.tool_mode == "erase":
            r = self.erase_radius
        elif self.tool_mode == "probe":
            r = self.probe_radius
        else:
            self.preview_ring.setVisible(False)
            return

        if r <= 0:
            self.preview_ring.setVisible(False)
            return

        self.preview_ring.setRect(x - r, y - r, 2 * r, 2 * r)
        self.preview_ring.setVisible(True)

    def on_mouse_moved(self, x: float, y: float):
        self._last_mouse_scene_pos = (x, y)
        self.lbl_pos.setText(f"x: {x:.1f}, y: {y:.1f}")
        self.update_tool_preview_ring(x, y)

    def on_mouse_clicked(self, x: float, y: float):
        if not self.project.image_path:
            return
        if x < 0 or y < 0 or x >= self.project.image_width or y >= self.project.image_height:
            return

        if self.tool_mode == "pan":
            return # Pan is handled by the canvas itself (DragMode.ScrollHandDrag)
        elif self.tool_mode == "probe":
            self.probe_at(int(x), int(y))
        elif self.tool_mode == "erase":
            self.erase_at(int(x), int(y))
        elif self.tool_mode == "brush":
            self.paint_at(int(x), int(y))
        elif self.tool_mode == "spray":
            self.spray_at(int(x), int(y))
        elif self.tool_mode == "entity_point":
            self.place_entity_point(int(x), int(y))

    def ensure_mask_image(self, layer: Layer, category_id: str) -> QtGui.QImage:
        b64 = layer.category_masks.get(category_id)
        if b64:
            img = png_base64_to_qimage(b64)
            if not img.isNull():
                return img.convertToFormat(QtGui.QImage.Format_RGBA8888)
        # create empty transparent mask
        img = QtGui.QImage(self.project.image_width, self.project.image_height, QtGui.QImage.Format_RGBA8888)
        img.fill(QtGui.QColor(0, 0, 0, 0))
        return img

    def on_brush_radius_changed(self, v: int):
        self.brush_radius = int(v)
        if self._last_mouse_scene_pos:
            self.update_tool_preview_ring(*self._last_mouse_scene_pos)

    def paint_at(self, x: int, y: int):
        """Paint a point in the mask of the current category."""
        # TODO: continuous painting on mouse-drag (not just click), with proper stroke interpolation so it doesn’t “dot” when moving fast.
        layer = self.current_layer()
        if not layer or not self.current_category_id:
            self.status.showMessage("Select a layer and a category to paint.", 2000)
            return
        cat = next((c for c in layer.categories if c.id == self.current_category_id), None)
        if not cat:
            return

        mask = self.ensure_mask_image(layer, cat.id)

        painter = QtGui.QPainter(mask)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)

        # paint with category color, but keep alpha strong for mask visibility
        qc = rgba_tuple_to_qcolor(cat.color)
        qc.setAlpha(200)

        brush = QtGui.QBrush(qc)
        painter.setBrush(brush)
        painter.setPen(QtCore.Qt.NoPen)
        r = self.brush_radius
        painter.drawEllipse(QtCore.QPointF(x, y), r, r)
        painter.end()

        layer.category_masks[cat.id] = qimage_to_png_base64(mask)
        self.update_overlay_for(layer.id, cat.id, mask)

    def on_erase_radius_changed(self, v: int):
        self.erase_radius = int(v)
        if self._last_mouse_scene_pos:
            self.update_tool_preview_ring(*self._last_mouse_scene_pos)

    def on_erase_mode_changed(self, _idx: int):
        self.erase_mode = self.combo_erase_mode.currentData()

    def erase_at(self, x: int, y: int):
        layer = self.current_layer()
        if not layer:
            self.status.showMessage("Select a layer to erase.", 2000)
            return

        r = self.erase_radius
        mode = self.erase_mode

        # For category-specific modes we need a selected category
        if mode in ("erase_only_category", "erase_all_but_category") and not self.current_category_id:
            self.status.showMessage("Select a category for this erase mode.", 2500)
            return

        def erase_circle(mask_img: QtGui.QImage):
            painter = QtGui.QPainter(mask_img)
            painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
            painter.setCompositionMode(QtGui.QPainter.CompositionMode_Clear)
            painter.setPen(QtCore.Qt.NoPen)
            painter.setBrush(QtGui.QBrush(QtGui.QColor(0, 0, 0, 255)))
            painter.drawEllipse(QtCore.QPointF(x, y), r, r)
            painter.end()

        # Determine which category masks to affect
        if mode == "erase_all":
            target_category_ids = [c.id for c in layer.categories]
        elif mode == "erase_only_category":
            target_category_ids = [self.current_category_id]
        elif mode == "erase_all_but_category":
            target_category_ids = [c.id for c in layer.categories if c.id != self.current_category_id]
        else:
            target_category_ids = []

        if not target_category_ids:
            return

        for cid in target_category_ids:
            # Skip categories that don't exist in this layer
            if not any(c.id == cid for c in layer.categories):
                continue

            mask = self.ensure_mask_image(layer, cid)
            erase_circle(mask)

            layer.category_masks[cid] = qimage_to_png_base64(mask)
            self.update_overlay_for(layer.id, cid, mask)

    def spray_at(self, x: int, y: int):
        """Spray paint a point in the mask of the current category."""
        # TODO
        pass

    def on_probe_radius_changed(self, v: int):
        self.probe_radius = int(v)
        if self._last_mouse_scene_pos:
            self.update_tool_preview_ring(*self._last_mouse_scene_pos)

    def probe_at(self, x: int, y: int):
        layer = self.current_layer()
        if not layer:
            self.status.showMessage("Select a layer for probing.", 2000)
            return

        r = self.probe_radius
        # Sample categories: compute % of pixels in radius that belong to each category mask
        results = []
        total = 0

        # pre-load masks
        masks = []
        for c in layer.categories:
            b64 = layer.category_masks.get(c.id)
            if not b64:
                continue
            img = png_base64_to_qimage(b64)
            if img.isNull():
                continue
            masks.append((c, img.convertToFormat(QtGui.QImage.Format_RGBA8888)))

        # circle bounds
        x0 = max(0, x - r)
        x1 = min(self.project.image_width - 1, x + r)
        y0 = max(0, y - r)
        y1 = min(self.project.image_height - 1, y + r)

        counts = {c.id: 0 for c, _ in masks}

        for yy in range(y0, y1 + 1):
            dy = yy - y
            for xx in range(x0, x1 + 1):
                dx = xx - x
                if dx * dx + dy * dy > r * r:
                    continue
                total += 1
                for c, img in masks:
                    a = QtGui.QColor(img.pixel(xx, yy)).alpha()
                    if a > 10:
                        counts[c.id] += 1

        if total > 0:
            for c, _img in masks:
                pct = (counts[c.id] / total) * 100.0
                if pct > 0.2:
                    results.append((pct, c.name))
            results.sort(reverse=True)

        # Entities inside radius
        ents_in = []
        for e in layer.entities:
            dx = e.x - x
            dy = e.y - y
            if dx * dx + dy * dy <= r * r:
                ents_in.append(e.name)

        lines = [f"Probe @ ({x},{y}) r={r}px"]
        if results:
            lines.append("Categories:")
            for pct, name in results[:8]:
                lines.append(f"  - {name}: {pct:.1f}%")
        else:
            lines.append("Categories: (none)")

        if ents_in:
            lines.append("Entities:")
            for name in ents_in[:12]:
                lines.append(f"  - {name}")
        else:
            lines.append("Entities: (none)")

        QtWidgets.QMessageBox.information(self, "Probe result", "\n".join(lines))

    def place_entity_point(self, x: int, y: int):
        layer = self.current_layer()
        if not layer or not self.current_entity_id:
            self.status.showMessage("Select an entity, then click to place it.", 2000)
            return
        ent = next((e for e in layer.entities if e.id == self.current_entity_id), None)
        if not ent:
            return
        ent.x = x
        ent.y = y
        self.rebuild_entities()

    # ---------- Overlay + entity rendering ----------
    def rebuild_overlays(self):
        # Remove all overlay items and re-add for current layer only
        for key, item in list(self.overlay_items.items()):
            self.scene.removeItem(item)
        self.overlay_items.clear()

        layer = self.current_layer()
        if not layer or not self.project.image_path:
            return

        # Z order: base image 0, overlays 10.., entities 100..
        z = 10
        for cat in layer.categories:
            b64 = layer.category_masks.get(cat.id)
            if not b64:
                continue
            img = png_base64_to_qimage(b64)
            if img.isNull():
                continue
            pm = QtGui.QPixmap.fromImage(img)
            item = self.scene.addPixmap(pm)
            item.setZValue(z)
            item.setOpacity(0.55)
            item.setPos(0, 0)
            self.overlay_items[(layer.id, cat.id)] = item
            z += 1

    def update_overlay_for(self, layer_id: str, category_id: str, mask_img: QtGui.QImage):
        key = (layer_id, category_id)
        if key in self.overlay_items:
            self.overlay_items[key].setPixmap(QtGui.QPixmap.fromImage(mask_img))
            return
        # if overlay didn't exist yet, rebuild overlays for layer
        self.rebuild_overlays()

    def rebuild_entities(self):
        # remove previous entity items
        for eid, item in list(self.entity_items.items()):
            self.scene.removeItem(item)
        self.entity_items.clear()

        layer = self.current_layer()
        if not layer or not self.project.image_path:
            return

        for e in layer.entities:
            # simple point: small circle + label
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
            self.entity_items[e.id] = group

    # ---------- Save / Load ----------
    def project_to_dict(self) -> dict:
        d = asdict(self.project)
        return d

    def dict_to_project(self, d: dict) -> Project:
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
                category_masks=dict(ld.get("category_masks", {})),
            )
            for cd in ld.get("categories", []):
                layer.categories.append(Category(id=cd["id"], name=cd["name"], color=tuple(cd["color"])))
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

    def persist_last_project_path(self, path):
        """Persist the last opened/saved project path"""
        self.settings.setValue("last_project_json", path)

    def load_last_project_on_startup(self):
        path = self.settings.value("last_project_json", "", type=str)
        if not path:
            return
        elif not os.path.exists(path):
            # stale setting; clean it up
            self.settings.remove("last_project_json")
            return
        else:
            self.load_project_json(path)

    def save_project_json(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Save project JSON", "", "JSON (*.json)")
        if not path:
            return
        data = self.project_to_dict()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        self.persist_last_project_path(path)
        self.status.showMessage(f"Saved: {path}", 2500)

    def load_project_json(self, path=None):
        if not path:
            path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Load project JSON", "", "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
            self.persist_last_project_path(path)
        except Exception as ex:
            QtWidgets.QMessageBox.warning(self, "Load failed", str(ex))
            return

        self.project = self.dict_to_project(d)
        self.refresh_layer_list(select_id=self.project.layers[0].id if self.project.layers else None)

        # reload base image if available
        if self.project.image_path and os.path.exists(self.project.image_path):
            img = QtGui.QImage(self.project.image_path)
            if not img.isNull():
                self.scene.clear()
                self.ensure_preview_ring()
                self.overlay_items.clear()
                self.entity_items.clear()
                self.base_pixmap_item = self.scene.addPixmap(QtGui.QPixmap.fromImage(img))
                self.base_pixmap_item.setZValue(0)
                self.scene.setSceneRect(0, 0, img.width(), img.height())
                self.canvas.resetTransform()
                self.canvas.fitInView(self.scene.sceneRect(), QtCore.Qt.KeepAspectRatio)
        else:
            self.scene.clear()
            self.ensure_preview_ring()
            self.overlay_items.clear()
            self.entity_items.clear()
            self.base_pixmap_item = None

        self.rebuild_overlays()
        self.rebuild_entities()
        self.status.showMessage(f"Loaded: {path}", 2500)

    # ---------- misc ----------
    def closeEvent(self, event: QtGui.QCloseEvent):
        # prototype: no dirty tracking
        super().closeEvent(event)

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
