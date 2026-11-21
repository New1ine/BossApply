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


def list_cards(root, min_fraction=0.18):
    """识别职位卡片，跳过广告；完整程度依据当前列表视口高度动态判断。"""
    lst = query(root, id="rv_list")
    if lst is None:
        raise RuntimeError("不是职位列表页面，需先在手机进入已筛选的岗位首页")
    left, top, right, bottom = rect(lst)
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
        x1, y1, x2, y2 = bounds
        complete = (bool(company) and left <= x1 < x2 <= right and top <= y1 < y2 <= bottom
                    and (y2 - y1) >= (bottom - top) * min_fraction)
        if key in cards and (cards[key]["complete"] or not complete):
            continue
        texts = [title]
        for child in parent.iter("node"):
            if child is not node and (text := child.get("text")):
                texts.append(text)
            if description := child.get("content-desc"):
                texts.append(description)
        cards[key] = {
            "key": key, "title": title, "company": company, "bounds": bounds, "texts": texts,
            "complete": complete,
        }
    return sorted(cards.values(), key=lambda c: c["bounds"][1])
