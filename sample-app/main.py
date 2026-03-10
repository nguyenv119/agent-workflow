from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

app = FastAPI(title="Sample App", version="0.1.0")

# In-memory data store for demonstration
ITEMS = [
    {"id": 1, "name": "Widget", "description": "A basic widget"},
    {"id": 2, "name": "Gadget", "description": "A handy gadget"},
    {"id": 3, "name": "Doohickey", "description": "A useful doohickey"},
]


class Item(BaseModel):
    id: int
    name: str
    description: str


@app.get("/health")
def health_check():
    """Return service health status."""
    return {"status": "ok"}


@app.get("/items", response_model=list[Item])
def list_items():
    """Return all items."""
    return ITEMS


@app.get("/items/{item_id}", response_model=Item)
def get_item(item_id: int):
    """Return a single item by id, or 404 if not found."""
    for item in ITEMS:
        if item["id"] == item_id:
            return item
    raise HTTPException(status_code=404, detail=f"Item {item_id} not found")
