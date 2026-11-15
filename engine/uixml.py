"""安卓 UI XML 的轻量查询与职位卡片解析。"""
import re


def rect(node):
    numbers = re.findall(r"\d+", node.get("bounds", ""))
    return tuple(map(int, numbers)) if len(numbers) == 4 else None


def middle(node):
    x1, y1, x2, y2 = rect(node)
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
    lx1, top, lx2, bottom = rect(lst)
    parents = {child: parent for parent in root.iter() for child in parent}
    cards, seen = [], set()
    for node in lst.iter("node"):
        if node_id(node) != "tv_position_name":
            continue
        parent = node
        while parent in parents and parent.get("clickable") != "true":
            parent = parents[parent]
        bounds = rect(parent)
        if bounds is None:
            continue
        title = node.get("text", "").replace(" &@ ", "").strip()
        company = next((n.get("text", "").strip() for n in parent.iter("node")
                       if node_id(n) == "tv_company_name"), "")
        key = (title, company)
        if key in seen:
            continue
        seen.add(key)
        x1, y1, x2, y2 = bounds
        cards.append({
            "key": key, "title": title, "company": company, "bounds": bounds,
            "complete": bool(company) and y1 >= top and y2 <= bottom
                        and (y2 - y1) >= (bottom - top) * min_fraction,
        })
    return sorted(cards, key=lambda c: c["bounds"][1])
