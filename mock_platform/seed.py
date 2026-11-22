"""Seed data for the fake shop. Prices are integer VND."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mock_platform.store import Platform

PRODUCTS = [
    ("p_tee", "Basic cotton T-shirt", "Plain cotton tee", [("v_tee_m_black", "M / black", 150_000, 20),
                                                          ("v_tee_l_black", "L / black", 150_000, 15),
                                                          ("v_tee_m_white", "M / white", 150_000, 0)]),
    ("p_shoes", "Running shoes", "Light running shoes", [("v_shoes_42", "size 42", 1_200_000, 5),
                                                          ("v_shoes_43", "size 43", 1_200_000, 3)]),
    ("p_mug", "Ceramic mug", "350 ml ceramic mug", [("v_mug", "default", 90_000, 50)]),
    ("p_lamp", "Desk lamp", "LED desk lamp, last unit demo", [("v_lamp", "default", 450_000, 1)]),
    ("p_bottle", "Steel water bottle", "750 ml insulated bottle", [("v_bottle_blue", "blue", 280_000, 12),
                                                                    ("v_bottle_grey", "grey", 280_000, 8)]),
    ("p_backpack", "Daypack", "20 L backpack", [("v_backpack", "default", 650_000, 7)]),
]

USERS = {
    "khang": ("khang123", "Pham Phu Khang"),
    "linh": ("linh123", "Linh"),
}

ADDRESSES = {
    "khang": [
        {"id": "addr_k1", "label": "Home", "line": "1 Dong Khoi, District 1", "region": "HCM"},
        {"id": "addr_k2", "label": "Office", "line": "10 Tran Hung Dao, Hoan Kiem", "region": "HN"},
    ],
    "linh": [{"id": "addr_l1", "label": "Home", "line": "5 Le Loi, Da Nang", "region": "DN"}],
}


def seed_data(p: "Platform") -> None:
    from mock_platform.store import Variant

    for pid, name, desc, variants in PRODUCTS:
        p.products[pid] = {"id": pid, "name": name, "description": desc}
        for vid, vname, price, stock in variants:
            p.variants[vid] = Variant(vid, pid, vname, price, stock)
    for username, (password, display) in USERS.items():
        p.users[username] = {"password": password, "name": display, "addresses": ADDRESSES[username]}
    p.clients["ucp"] = {
        "id": "ucp",
        "secret": "ucp-mock-secret",
        "redirect_uris": ["http://localhost:8000/oauth/callback"],
    }
