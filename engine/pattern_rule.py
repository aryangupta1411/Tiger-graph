"""Pattern label — PLAN §4.4 (detectors first, then the generator convention verified in §4.1.5).

label(chain_members, card_modal_region, flags) -> pattern

  chain_members : the EPISODE members (episode_candidates.chain rows restricted to the episode),
                  each {id, channel, addr1, amt, device_new, ...}
  card_modal_region : the card's modal in-person region computed at ts <= opened_at
  flags : scorecard flags; the detector flags used here are
            ring_hit            strong anonymous-proxy ring profile on the episode      → undocumented
            burst_hit           >= 4 x $450-499.99 online within 40 min                  → undocumented
            card_testing_chain  all-online episode, >= 5 members, >= 1 sub-$5 member    → card_testing

out_of_region_use = an all-card-present episode that leaves the card's modal (home) region. "No history" in the
README's wording is read as "away from home", which is how the bank labels it: 581 of the 955 confirmed
out_of_region_use closed cases had every transaction in a region the card had used >= 5 times before, and only 173
touched a never-used region (tests/unit/test_engine_r2.py). A "region must be new" rule would mislabel most of them.
"""
from __future__ import annotations

from engine import config


def label(chain_members: list[dict], card_modal_region: str, flags: dict) -> str:
    if not chain_members:
        return "none"
    if flags.get("ring_hit") or flags.get("burst_hit"):
        return "undocumented"
    channels = {m.get("channel") for m in chain_members}
    all_online = channels == {"online"}
    all_in_person = channels == {"in_person"}
    n_small = sum(1 for m in chain_members if m.get("channel") == "online" and float(m.get("amt", 0)) < config.CARD_TESTING_SMALL)
    if all_online and len(chain_members) >= 5 and n_small >= 1:
        return "card_testing"
    if flags.get("card_testing_chain") and all_online:
        return "card_testing"
    if "online" in channels and "in_person" in channels:
        return "account_takeover"
    if all_in_person:
        if card_modal_region and all(str(m.get("addr1", "")) == str(card_modal_region) for m in chain_members):
            return "account_takeover"
        return "out_of_region_use"
    # all online
    if any(m.get("device_new") == "New" for m in chain_members):
        return "card_not_present_new_device"
    return "card_not_present_fraud"


def episode_flags(chain_members: list[dict], card_modal_region: str) -> dict:
    """Convenience: the three structural flags the Scorecard records for the episode."""
    channels = {m.get("channel") for m in chain_members}
    return {
        "mixed_channel": ("online" in channels and "in_person" in channels),
        "any_new_member": any(m.get("device_new") == "New" for m in chain_members),
        "all_in_modal_region": bool(chain_members) and channels == {"in_person"}
        and all(str(m.get("addr1", "")) == str(card_modal_region) for m in chain_members),
    }
