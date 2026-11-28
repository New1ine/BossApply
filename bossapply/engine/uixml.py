"""安卓 UI XML 的轻量查询与职位卡片解析。"""
import re


_BOUNDS = re.compile(r"\[([0-9]{1,10}),([0-9]{1,10})\]\[([0-9]{1,10}),([0-9]{1,10})\]")


def rect(node):
    match = _BOUNDS.fullmatch(node.get("bounds", ""))
    if match is None:
        return None
    x1, y1, x2, y2 = map(int, match.groups())
    if x1 < x2 and y1 < y2:
        return x1, y1, x2, y2
    return None


def middle(node):
    bounds = rect(node)
    if bounds is None:
        raise ValueError("控件坐标无效，无法点击")
    x1, y1, x2, y2 = bounds
    return (x1 + x2) // 2, (y1 + y2) // 2


def node_id(node):
    return node.get("resource-id", "").rsplit("/", 1)[-1]


def query(root, *, id=None, text=None, contains=None):
    for node in root.iter("node"):
        if id is not None and node_id(node) != id:
            continue
        value = node.get("text", "")
        if text is not None and value != text:
            continue
        if contains is not None and contains not in value:
            continue
        if rect(node) is not None:
            return node
    return None


def queries(root, id):
    return [n for n in root.iter("node") if node_id(n) == id and rect(n)]


_CARD_ANCHORS = (
    "tv_position_name", "tv_salary_statue", "tv_company_name", "fl_require_info",
    "tv_employer", "tv_active_status", "tv_distance",
)


def _inside(bounds, outer, *, vertical_margin=False):
    if bounds is None:
        return False
    left, top, right, bottom = outer
    x1, y1, x2, y2 = bounds
    return (left <= x1 < x2 <= right
            and (top < y1 < y2 < bottom if vertical_margin else top <= y1 < y2 <= bottom))


def _card_visibility(parent, title_node, viewport, bounds, company):
    """Clipped accessibility bounds cannot prove a card's original height.

    A card clear of the viewport edges has an observable whole container. At
    an edge, only the known job-card template can establish that its readable
    content (including the footer) fits. Unknown edge layouts remain pending
    until scrolling brings them into the interior of the list.
    """
    nodes = {}
    for child in parent.iter("node"):
        child_id = node_id(child)
        if child_id in _CARD_ANCHORS and child_id not in nodes and rect(child) is not None:
            nodes[child_id] = child
    anchors = {child_id: rect(child) for child_id, child in nodes.items()}
    content = query(parent, id="view_job_card")
    if content is None:
        content = query(parent, id="boss_job_card_view")
    content_bounds = None if content is None else rect(content)
    _, top, _, bottom = viewport
    touches_top, touches_bottom = bounds[1] <= top, bounds[3] >= bottom
    complete = bool(company) and _inside(bounds, viewport)
    reason = "inside_viewport"
    if complete and (touches_top or touches_bottom):
        # In the observed template the employer and distance are the footer.
        # Activity text is optional (for example, campus direct-application
        # cards omit it). Requiring a full footer avoids treating an empty
        # requirements list on a bottom half-card as a known absence of tags.
        required = ("tv_position_name", "tv_company_name", "tv_employer", "tv_distance")
        complete = (
            content_bounds is not None
            and _inside(content_bounds, bounds)
            and _inside(content_bounds, viewport, vertical_margin=True)
            and all(child_id in nodes
                    and (nodes[child_id].get("text") or nodes[child_id].get("content-desc", "")).strip()
                    for child_id in required)
            and all(_inside(anchor, content_bounds) and _inside(anchor, viewport, vertical_margin=True)
                    for anchor in anchors.values())
            and anchors.get("tv_position_name", (0, 0, 0, 0))[3]
                <= anchors.get("tv_company_name", (0, 0, 0, 0))[1]
            and anchors.get("tv_company_name", (0, 0, 0, 0))[3]
                <= anchors.get("tv_employer", (0, 0, 0, 0))[1]
            and anchors.get("tv_employer", (0, 0, 0, 0))[1]
                <= anchors.get("tv_distance", (0, 0, 0, 0))[1]
        )
        reason = "visible_template_content" if complete else "edge_content_unconfirmed"
    elif not complete:
        reason = "missing_company_or_outside_viewport"
    return {
        "complete": bool(complete), "complete_reason": reason, "viewport": viewport,
        "title_bounds": rect(title_node), "content_bounds": content_bounds,
        "touches_top": touches_top, "touches_bottom": touches_bottom, "anchors": anchors,
    }


def list_cards(root):
    """识别职位卡片，跳过广告；仅将可确认未裁剪的卡片用于筛选。"""
    lst = query(root, id="rv_list")
    if lst is None:
        raise RuntimeError("不是职位列表页面，需先在手机进入已筛选的岗位首页")
    viewport = rect(lst)
    parents = {child: parent for parent in lst.iter() for child in parent}
    cards = {}
    for node in lst.iter("node"):
        if node_id(node) != "tv_position_name":
            continue
        parent = node
        while parent is not lst and parent.get("clickable") != "true":
            parent = parents[parent]
        if parent is lst:
            continue
        bounds = rect(parent)
        if bounds is None:
            continue
        title = node.get("text", "").replace(" &@ ", "").strip()
        if not title:
            continue
        company = next((n.get("text", "").strip() for n in parent.iter("node")
                       if node_id(n) == "tv_company_name"), "")
        key = (title, company)
        visibility = _card_visibility(parent, node, viewport, bounds, company)
        complete = visibility["complete"]
        texts = [title]
        salary = ""
        requirements = []
        for child in parent.iter("node"):
            child_id = node_id(child)
            if child_id == "tv_salary_statue" and not salary:
                salary = (child.get("text") or child.get("content-desc", "")).strip()
            if child_id == "fl_require_info":
                requirements.extend(
                    label for tag in child.iterfind(".//node")
                    if (label := (tag.get("text") or tag.get("content-desc", "")).strip())
                )
            if child is not node and (text := child.get("text")):
                texts.append(text)
            if description := child.get("content-desc"):
                texts.append(description)
        card = {
            "key": key, "title": title, "company": company, "bounds": bounds, "texts": texts,
            "salary": salary, "requirements": requirements, **visibility,
        }
        previous = cards.get(key)
        if previous is not None:
            # A partial copy may gain fields after clipping, so missing values
            # are not a conflict. Conflicting visible data, however, must not
            # silently merge two different postings into one processed key.
            salary_conflict = previous["salary"] and salary and previous["salary"] != salary
            requirements_conflict = (
                previous["complete"] and complete
                and set(previous["requirements"]) != set(requirements)
            )
            if salary_conflict or requirements_conflict:
                raise RuntimeError(
                    f"同公司同名职位存在不同薪资或要求，无法可靠区分：{company} / {title}。"
                    "请调整筛选范围后重新开始，避免漏查或重复投递。"
                )
            if previous["complete"] or not complete:
                continue
        cards[key] = card
    return sorted(cards.values(), key=lambda c: c["bounds"][1])
