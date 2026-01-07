# Polygon requirements
I want to implement Point, Line and Polygon. Here is an outline of the whole requirements for the three tools.

A) Datastructure
- Point, Line and Polygon are composed of Dots and are called an entity of a kind.
- An entity is stored at the layer level in a list.
- A Dot itself has x,y,radius, optional name, optional json data (k,v). 
- - x,y,r are floats and can be finer grained than a pixel of the image. 
- - The radius can be > 0. 0 being a true cosmetic pixel (default)
- - radius bigger than 0 is a transparent circle with that radius true to the image.
- - a dot itself can have name, but is empty per default
- - the dot data contains {"rgba": [0,0,0,255]} indicating the default color black (solid)
- An Entity (Point, Line and Polygon) has a required name and optional description and optional json data (k,v)
- - Store as unified entity types EntityBase(type="point|line|polygon", id, name, description?, data?, dots=[Dot...], closed=False?)
- - EntityPoint: Single Dot.
- - EntityLine: Start Dot, End Dot and list of Path Dots in between (can all be in a list)
- - EntityPolygon: OriginDot (start & end) and list of path dots (can all be in a list)
- No migration from old datastructures needed (-> cold reset/wipe) no old project data with entities stored

Currently the entity can be added via the Layer entity list. Now the list is for info/edit/delete purposes only.
A new entity should be added via the toolbar (remove the add entity button from the Layer entity list).

B) Tool implementation
1) Display
- dots are solid black per default or according to the dot.data.rgba
- lines between dots are fine black lines (EntityLine & EntityPolygon)
- - the width of the line between dot a and b is equal to the radius of dot a
- Dots and lines should be visible (but faint) at all zoom levels of the image
- if a dot has a radius > 0 the dot is displayed as a transparent circle with that radius true to the image.
- A selected entity is displayed blue; a selected dot is displayed red
2) Parameterization
- In the tool box some parameter can be preset, so click and add entities of one kind rapidly is possible 
- Point: dot radius=1, dot color: black, entity name, entity json data
- Line: dot radius=1, dot color: black, entity name, entity json data
- Polygon: dot radius=1, dot color: black, entity name, entity json data
3) Create:
- Select Point, Line, Polygon
- Click to add point or start/origin of line/poligon
- Point: Dialog: Set name, x,y,r and opt. entity description, and opt. json data
- Line: continue click to add subsequent dots; a ctrl+click adds the end point -> Dialog set name, descr, data of entity
- Polygon: continue click to add subsequent dots; a ctrl+click adds a last point -> Dialog set name, descr, data of entity
- - Ctrl+click places final vertex and auto-close to origin.
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
5) Undo/redo integration

## Implementation plan
1) Lets only implement the datastructures (Dot, EntityPoint, EntityLine, EntityPolygon) and the routines to insert/update the layer entity list. datastructures + codec
2) vector renderer + selection/hit-test + inspector plumbing
3) Lets implement the Point tool (Display, Param, Create, Edit)
4) lets implement the Line tool (Display, Param, Create, Edit)
5) lets implement the Polygon tool (Display, Param, Create, Edit)


Are we ready for step 3? If yes: Lets implement the Point tool (Display, Param, Create, Edit)
Here is the current state of prototype v7 as the basis for the next patch.