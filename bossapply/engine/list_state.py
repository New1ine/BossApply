"""Read visible list-status signals without interpreting job text as status.

The recorded BOSS snapshots expose ``refresh_layout`` even when idle, but no
loading footer was captured. Container IDs alone therefore never prove that a
request is running. These rules recognize explicit visible status labels and
progress widgets; an absent signal remains unknown, rather than "loaded".
"""
from dataclasses import dataclass
import re
import unicodedata

from .uixml import node_id, query, rect


_STATUS_ID = re.compile(r"(?:^|_)(?:load(?:ing|more)?|refresh|footer|retry|error|empty|progress|status|state)(?:_|$)")
_LOADING = re.compile(r"(?:正在(?:为您)?加载(?:更多)?中?|加载中|加载(?:更多)?[.。…]+|loading(?:more)?)[.。…]*", re.I)
_END = re.compile(r"(?:没有更多(?:职位|岗位|数据|内容)?了?|暂无更多(?:职位|岗位|数据)?|无更多(?:职位|岗位|数据)|(?:已经|已)?到底了?|已(?:全部|经全部)加载|全部加载完毕|nomore(?:jobs|data|items)?)", re.I)
_RETRY = re.compile(r"(?:加载失败(?:(?:请|点击)?重试)?|(?:网络异常|网络错误|网络连接失败)(?:(?:请|点击)?(?:重试|重新加载))?|点击重试|重新加载|retry|(?:loading|load)failed(?:(?:tapto)?retry)?)[.。…]*", re.I)


@dataclass(frozen=True)
class JobListState:
    loading: bool = False
    end: bool = False
    retry: bool = False
    signals: tuple = ()

    @property
    def known(self):
        return bool(self.signals)

    @property
    def token(self):
        return self.loading, self.end, self.retry, self.signals


def _label(value):
    value = unicodedata.normalize("NFKC", value).strip()
    # Keep ellipses for "加载...", but allow ordinary separator punctuation.
    return re.sub(r"[\s，,：:！!；;]+", "", value)


def _semantic_id(node):
    value = re.sub(r"(?<=[a-z])(?=[A-Z])", "_", node_id(node)).lower()
    return _STATUS_ID.search(value) is not None


def _visible(node):
    return node.get("visible-to-user") != "false" and node.get("displayed") != "false" and rect(node) is not None


def _near_list(bounds, viewport):
    left, top, right, bottom = viewport
    x1, y1, x2, y2 = bounds
    margin = max(24, (bottom - top) * .12)
    return x1 < right and x2 > left and y1 < bottom + margin and y2 > top - margin


def job_list_state(root):
    """Return explicit loading/end/retry signals; no signal means unknown.

    Status labels must be in a semantic status control or near the list's
    footer. Progress widgets must overlap the list or its immediate boundary.
    Nodes inside a clickable job-card container are excluded altogether.
    """
    listing = query(root, id="rv_list")
    if listing is None:
        return JobListState()
    viewport = rect(listing)
    parents = {child: parent for parent in root.iter() for child in parent}
    list_nodes = set(listing.iter("node"))
    card_nodes = set()
    for title in listing.iter("node"):
        if node_id(title) != "tv_position_name":
            continue
        ancestor = title
        card_root = None
        while ancestor is not listing:
            if ancestor.get("clickable") == "true":
                card_root = ancestor
            ancestor = parents[ancestor]
        if card_root is not None:
            card_nodes.update(card_root.iter("node"))

    signals = set()
    for node in root.iter("node"):
        if node in card_nodes or not _visible(node):
            continue
        bounds = rect(node)
        if not _near_list(bounds, viewport):
            continue
        ancestors = []
        ancestor = node
        while ancestor is not None:
            ancestors.append(ancestor)
            if ancestor is listing:
                break
            ancestor = parents.get(ancestor)
        if any(item.get("visible-to-user") == "false" or item.get("displayed") == "false"
               for item in ancestors):
            continue
        semantic = any(_semantic_id(item) for item in ancestors[:4])
        inside = node in list_nodes
        footer = bounds[1] >= viewport[1] + (viewport[3] - viewport[1]) * .65
        progress = node.get("class", "").rsplit(".", 1)[-1].lower().endswith("progressbar")
        if progress and (inside or semantic):
            signals.add(("loading", node_id(node), "", bounds))
        if not (semantic or footer):
            continue
        text = _label(node.get("text", "") or node.get("content-desc", ""))
        if not text:
            continue
        kind = "loading" if _LOADING.fullmatch(text) else "end" if _END.fullmatch(text) else "retry" if _RETRY.fullmatch(text) else None
        if kind is not None:
            signals.add((kind, node_id(node), text, bounds))
    ordered = tuple(sorted(signals))
    kinds = {signal[0] for signal in ordered}
    loading = "loading" in kinds
    return JobListState(loading=loading, end=not loading and "end" in kinds,
                        retry=not loading and "retry" in kinds, signals=ordered)
