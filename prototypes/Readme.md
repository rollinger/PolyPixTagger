Below is a solid prototype architecture + a runnable PySide6/Qt “skeleton app” that matches your PixTag requirements and already supports:

-Import image
-Multiple layers
-Per-layer categories
-Per-layer entities
-Paint pixels into a category (simple brush on an RGBA mask)
-Probe (point + radius) → shows category coverage + entities in that area
-Save project to JSON (image path + layers + categories + entities + masks as PNG base64)

It’s deliberately “prototype-grade”: clean enough to extend, but not over-engineered.

PySide6 Doc: [https://doc.qt.io/qtforpython-6/index.html]

Compact Doc: [https://natuurkundepracticumamsterdam.github.io/PySide6-compact-documentation/QtWidgets/]

# Data model (JSON-first)

Conceptually:
-Project
--image_path
--layers[]

-Layer
--name
--categories[] (e.g. “forest”)
--entities[] (e.g. “Doi Inthanon Forest” with props JSON)
--category_masks: map category_id → painted mask image (RGBA, we store as png base64)
--geometry_items[] (later: polygons/lines/points; prototype: entities are points)
-Category
--id, name, color
-Entity
--id, name
--category_id (optional)
--props (arbitrary JSON)
--shape (prototype: point only; later: polygon/line)

# Prototype UI layout (matches your spec)

-Top row: menu + toolbar (import/save + tool mode brush/probe + brush size)
--Center: QSplitter
--Left pane: image canvas (QGraphicsView) with base image + per-category mask overlays + entity points
--Right pane: layer list + category list + entity list + JSON editor for entity props
-Bottom row: read-only status (pos, layer, category, tool, etc.)