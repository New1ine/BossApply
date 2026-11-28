"""Plan list gestures from the observed viewport and cards, then verify the result."""
from dataclasses import dataclass
from statistics import median


@dataclass(frozen=True)
class ListSwipe:
    x: int
    start: int
    end: int
    duration: int
    viewport: tuple
    anchor: tuple | None
    unread_anchor: bool


def find_anchor(cards, key):
    if key is None:
        return None
    matches = [card for card in cards if card["key"] == key]
    if not matches and not key[1]:
        # The company can enter the accessibility tree only after a partial card moves.
        matches = [card for card in cards if card["title"] == key[0]]
    return matches[0] if len(matches) == 1 else None


def reading_geometry(cards, viewport):
    height = viewport[3] - viewport[1]
    heights = [card["bounds"][3] - card["bounds"][1] for card in cards
               if card["complete"] and not card.get("touches_top") and not card.get("touches_bottom")]
    card_height = median(heights) if heights else height / 3
    padding = max(8, round(min(card_height, height) * .08))
    # A readable leading card need not be pixel-aligned with the viewport edge.
    band = max(padding, min(card_height / 2, height * .15))
    return card_height, padding, band


def in_reading_band(card, cards, viewport):
    _, _, band = reading_geometry(cards, viewport)
    content = card.get("content_bounds") or card["bounds"]
    return card["complete"] and content[1] <= viewport[1] + band


def observed_list_motion(before, after):
    """Measure common visible content, excluding clipped bounds and identity changes."""
    movements = []
    for old in before:
        current = find_anchor(after, old["key"])
        if current is None:
            continue
        candidates = []
        for name in ("tv_position_name", "tv_company_name"):
            first = old.get("anchors", {}).get(name)
            second = current.get("anchors", {}).get(name)
            if first and second:
                candidates.append((first, second))
        if not candidates and not old.get("touches_top") and not current.get("touches_top"):
            candidates.append((old["bounds"], current["bounds"]))
        for first, second in candidates:
            old_view, new_view = old.get("viewport"), current.get("viewport")
            if old_view and not old_view[1] < first[1] < first[3] < old_view[3]:
                continue
            if new_view and not new_view[1] < second[1] < second[3] < new_view[3]:
                continue
            movements.append(first[1] - second[1])
            break
    return median(movements) if movements else None


def plan_job_swipe(cards, processed, viewport, *, component, anchor=None, touch_slop=None,
                   advance_scale=1.0):
    left, top, right, bottom = viewport
    height = bottom - top
    if right - left < 4 or height < 40:
        raise RuntimeError("职位列表可用区域太小，无法安全滑动")
    card_height, padding, _ = reading_geometry(cards, viewport)
    target = find_anchor(cards, anchor)
    if target is None:
        target = next((card for card in cards if card["key"] not in processed), None)
    unread = target is not None and target["key"] not in processed
    if target is None and cards:
        target = cards[-1]  # Keep an observed card as overlap while revealing new items.
    if target is not None:
        target_top = (target.get("content_bounds") or target["bounds"])[1]
        if not target["complete"] and target["bounds"][1] <= top:
            desired = -min(card_height * .65, height * .35)
        else:
            desired = target_top - (top + padding)
            if desired <= padding:
                desired = min(card_height, height * .45)
    else:
        desired = height * .45
    margin = max(12, round(height * .08))
    # Do not outrun server-side prefetch: reveal the next items with one slow
    # drag, retaining overlap. Loading reduces subsequent advances further.
    distance = min(abs(desired), height - 2 * margin) * advance_scale
    minimum = 2 * touch_slop + 1 if touch_slop is not None else max(16, round(height * .04))
    if minimum > height - 2 * margin:
        raise RuntimeError("职位列表空间不足以执行拖动，已停止以避免误点击")
    distance = min(height - 2 * margin, max(minimum, round(distance)))
    upward = desired >= 0
    start = bottom - margin if upward else top + margin
    # Touch just inside the observed content edge. Outer card padding can be
    # ignored by the hit target, while the centre may contain nested controls.
    content_lefts = [card["content_bounds"][0] for card in cards if card.get("content_bounds")
                     and left + 2 < card["content_bounds"][0] < left + (right - left) / 3]
    inset = max(8, touch_slop or round((right - left) * .025)) + 8
    x = min(content_lefts) + inset if content_lefts else left + round((right - left) * .1)
    x = min(right - 1, max(left + 1, x))
    # Start over an observed text label, rather than a fixed y that may land
    # on card padding, a company link or a nested requirements carousel.
    for name in ("tv_position_name", "tv_company_name"):
        candidates = []
        for card in cards:
            bounds = card.get("anchors", {}).get(name)
            if not bounds or bounds[3] - bounds[1] < max(8, touch_slop or 8):
                continue
            lo = max(top + margin + (minimum if upward else 0), bounds[1] + 1)
            hi = min(bottom - margin - (0 if upward else minimum), bounds[3] - 1)
            if lo <= hi and left < bounds[0] < bounds[2] <= right:
                y = min(hi, max(lo, (bounds[1] + bounds[3]) // 2))
                candidates.append(((bounds[0] + bounds[2]) // 2, y))
        if candidates:
            x, start = (max if upward else min)(candidates, key=lambda point: point[1])
            break
    available = start - top - margin if upward else bottom - margin - start
    distance = min(distance, available)
    if distance < minimum:
        raise RuntimeError("未确认足够的可拖动职位区域，已停止以避免误点击")
    end = start - distance if upward else start + distance
    speed = height * .25
    duration = max(280, min(5000, round(distance / speed * 1000)))
    return ListSwipe(x, start, end, duration, tuple(viewport),
                     target["key"] if target is not None else None, unread)
