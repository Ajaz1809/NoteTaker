# utils/flow_loader.py


from models.models import ConversationalFlow, FlowNode, FlowSubNode, FlowEdge, FlowGlobalSetting
from sqlalchemy.orm import joinedload

from db.database import SessionLocal   # import your session factory

def load_flow_from_db(flow_id: int):
    db = SessionLocal()   # <-- create DB session manually
    try:
        flow = db.query(ConversationalFlow).filter(ConversationalFlow.id == flow_id).first()
        if not flow:
            raise ValueError(f"Flow with id {flow_id} not found")

        # Eager load subnodes to avoid N+1 queries
        nodes = (
            db.query(FlowNode)
            .filter(FlowNode.flow_id == flow_id)
            .options(joinedload(FlowNode.subnodes))
            .all()
        )
        edges = db.query(FlowEdge).filter(FlowEdge.flow_id == flow_id).all()
        settings = db.query(FlowGlobalSetting).filter(FlowGlobalSetting.flow_id == flow_id).first()

        # --- build return JSON exactly as API ---
        node_list = []
        for n in nodes:
            # Use preloaded subnodes instead of querying separately
            subnodes = n.subnodes

            fields = [{"id": s.uuid, "value": s.value} for s in subnodes]
            subNodes = [
                {
                    "id": s.uuid,
                    "parentId": n.uuid,
                    "value": s.value,
                    "handleId": s.handle_id,
                }
                for s in subnodes
            ]

            prompt = n.prompt
            

            node_data = {
                "label": n.label,
                "description": n.description,
                "prompt": prompt,
                "fields": fields,
                "subNodes": subNodes,
                "settings": n.settings or {},
            }

            node_list.append({
                "id": n.uuid,
                "type": n.type,
                "position": {"x": n.position_x or 0.0, "y": n.position_y or 0.0},
                "data": node_data,
            })

        edge_list = [
            {
                "id": e.uuid,
                "source": e.source_node_id,
                "target": e.target_node_id,
                "type": e.type,
                "animated": e.animated,
                "sourceHandle": e.source_handle,
                "subNodeConnection": e.subnode_connection,
            }
            for e in edges
        ]

        return {
            "flow_id": flow.id,
            "uuid": flow.uuid,
            "name": flow.name,
            "description": flow.description,
            "nodes": node_list,
            "edges": edge_list,
            "globalSettings": settings.settings if settings else {},
        }

    finally:
        db.close()

