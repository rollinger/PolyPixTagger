I want to implement Point, Line and Polygon. Here is an outline of the requirements for the three tools.
A) Datastructure
- Point, Line and Poligon are composed of Dots and are called Entity.
- An entity is stored at the layer level in a list.
- A Dot itself has x,y,radius, optional name, optional json data (k,v). 
- - x,y,r are floats and can be finer grained than a pixel of the image. 
- - The radius can be 0 being a true point (default)
- - a dot itself can have name, but is empty per default
- - the dot data contains {"rgba": [255,255,255,255]} indicating the default color black
- An Entity (Point, Line and Polygon) has a required name and optional description and optional json data (k,v)
- EntityPoint: Single Dot.
- EntityLine: Start Dot, End Dot and list of Path Dots in between (can all be in a list)
- EntityPolygon: OriginDot (start & end) and list of path dots (can all be in a list)

B) Tool implementation
1) Display
- dots are solid black per default or according to the dot.data.rgba
- lines between dots are fine black lines (EntityLine & EntityPolygon)
- Dots and lines should be visible (but faint) at all zoom levels of the image
- if a dot has a radius > 0 the dot is displayed as a transparent circle with that radius true to the image.
- A selected entity is displayed blue; a selected dot is displayed red
2) Parameterization
- In the tool box some parameter can be preset, so click and add entities of one kind rapidly is possible 
- Point: dot radius=0, dot color, entity name, entity json data
- Line:
- Polygon:
3) Create:
- Select Point, Line, Polygon
- Click to add point or start/origin of line/poligon
- Point: Dialog: Set name, x,y,r and opt. entity description, and opt. json data
- Line: continue click to add subsequent dots; a ctrl+click adds the end point -> Dialog set name, descr, data of entity
- Polygon: continue click to add subsequent dots; a ctrl+click adds a last point -> Dialog set name, descr, data of entity
-> Save to Layer entity list & update layer.
4) Edit
- In the Layer entity list select the entity 
-- screen centers around the entity and the entity is highlighted blue.
-- The entity property pane shows the entity data plus all dot data. (editable; apply -> save & update layer)
-- Click on a dot (vicinity) and the dot is highlighted red; click and drag the point is moved until release (instant save & update layer)
--- The entity property pane scrolls to the selected dot (to edit dot related data)
-- Alt+click on a dot: delete dot instant save & update layer)
--- If the last dot was deleted in the entity, the entity is deleted (instant save & update layer)
-- Ctrl+click on the map with a dot selected: insert dot after the selected dot. (instant save & update layer)


First lets only implement the datastructures (Dot, EntityPoint, EntityLine, EntityPolygon) and the routines to insert/update the layer entity list.
Second lets implement the Point tool (Display, Create, Edit)