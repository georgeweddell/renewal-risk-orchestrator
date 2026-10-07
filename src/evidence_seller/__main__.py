"""Run the seller: python -m evidence_seller  (listens on http://127.0.0.1:8402)."""

import uvicorn

from evidence_seller.app import PORT, app

uvicorn.run(app, host="127.0.0.1", port=PORT)
