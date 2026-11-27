import time
import uuid
from mm import backends, config, ddb, matching
from mm.metrics import emit_metric


def join_queue():
    ticket_id = str(uuid.uuid4())
    now = int(time.time())

    ddb.queue.put_item(Item={
        "ticketId": ticket_id,
        "status": "waiting",
        "queuedAt": now,
        "expiresAt": now + config.TICKET_TTL_SECONDS,
    })

    emit_metric("TicketsCreated", 1)

    matching.try_form_match()

    return {"ticketId": ticket_id, "status": "waiting"}

def poll_ticket(ticket_id):
    ticket = ddb.queue.get_item(Key = {"ticketId": ticket_id}).get("Item")
    if not ticket:
        return {"status": "expired"}

    if ticket["status"] == "waiting":
        return {"status": "waiting"}

    session_id = ticket.get("sessionId")
    if not session_id:
        return {"status": "provisioning"}

    session = ddb.sessions.get_item(Key = {"sessionId": session_id}).get("Item")
    if not session:
        return {"status": "expired"}

    address = backends.active.resolve_address(session)
    if not address:
        return {"status": "provisioning"}

    ip, port = address
    return {
        "status": "matched",
        "sessionId": session_id,
        "ip": ip,
        "port": port,
    }

def leave_queue(ticket_id):
    ddb.queue.delete_item(Key = {"ticketId": ticket_id})
    return {"status": "cancelled"}
