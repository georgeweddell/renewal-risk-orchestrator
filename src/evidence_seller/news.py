"""The seller's catalogue: recent headlines by company domain (the CRM's `domain` field).

Shared with the evidence server's mock backend, which simulates this seller without a network or wallet.
"""

NEWS = {
    "halcyonrobotics.example": [
        {"date": "2026-09-14", "headline": "Halcyon Robotics announces a hiring freeze across its operations team"},
        {"date": "2026-08-30", "headline": "Halcyon Robotics' CFO departs; interim CFO named"},
    ],
    "kestrellogistics.example": [
        {"date": "2026-09-21", "headline": "Kestrel Logistics opens two new regional hubs"},
    ],
}
