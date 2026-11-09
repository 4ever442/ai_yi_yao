"""图谱层：加载、实体链接、邻域与子图。

这一层是问答的"锚点"，也是前端的导航骨架：
问题里的字面词先被图谱锚定成实体，后面的检索、校验、可视化都围绕实体展开。
"""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent / "data"


class Graph:
    def __init__(self, path: Path | None = None) -> None:
        raw = json.loads((path or DATA / "graph.json").read_text(encoding="utf-8"))
        self.meta = raw["meta"]
        self.types = {t["id"]: t for t in raw["types"]}
        self.type_list = raw["types"]
        self.predicates = {p["id"]: p["label"] for p in raw["predicates"]}
        self.pred_list = raw["predicates"]
        self.systems = raw["systems"]
        self.fields = raw["fields"]
        self.stats = raw["stats"]

        self.nodes: dict[str, dict] = {n["id"]: n for n in raw["nodes"]}
        self.edges: list[dict] = raw["edges"]

        self.out: dict[str, list[dict]] = defaultdict(list)
        self.inc: dict[str, list[dict]] = defaultdict(list)
        for e in self.edges:
            self.out[e["h"]].append(e)
            self.inc[e["t"]].append(e)

        self.by_type: dict[str, list[dict]] = defaultdict(list)
        for n in self.nodes.values():
            self.by_type[n["t"]].append(n)
        for lst in self.by_type.values():
            lst.sort(key=lambda n: -n["d"])

        # 别名词典：长词优先，避免「糖尿病」抢走「妊娠期糖尿病」
        self._alias: list[tuple[str, str]] = []
        for nid, n in self.nodes.items():
            for f in {n["n"], *n.get("a", [])}:
                f = (f or "").strip().lower()
                if len(f) >= 2:
                    self._alias.append((f, nid))
        self._alias.sort(key=lambda x: -len(x[0]))
        self._alias_set = dict(self._alias)

    # -------------------------------------------------- 基础访问
    def node(self, nid: str) -> dict | None:
        return self.nodes.get(nid)

    def name(self, nid: str) -> str:
        n = self.nodes.get(nid)
        return n["n"] if n else nid

    def type_label(self, tid: str) -> str:
        return self.types.get(tid, {}).get("label", tid)

    def has_triple(self, h: str, rel: str, t: str) -> bool:
        return any(e["r"] == rel and e["t"] == t for e in self.out.get(h, []))

    def rel_label(self, rid: str) -> str:
        return self.predicates.get(rid, rid)

    # -------------------------------------------------- 实体链接
    def link(self, text: str, limit: int = 12) -> list[dict]:
        """最长优先、互不重叠的实体提及识别。"""
        low = (text or "").lower()
        if not low:
            return []
        found: list[tuple[int, int, str, str]] = []
        for alias, nid in self._alias:
            start = 0
            while True:
                i = low.find(alias, start)
                if i < 0:
                    break
                found.append((i, i + len(alias), nid, alias))
                start = i + 1

        found.sort(key=lambda m: (-(m[1] - m[0]), m[0]))
        taken = [False] * len(low)
        chosen: list[dict] = []
        for s, e, nid, alias in found:
            if any(taken[s:e]):
                continue
            for i in range(s, e):
                taken[i] = True
            chosen.append({"node_id": nid, "alias": alias, "start": s, "end": e})

        chosen.sort(key=lambda m: m["start"])
        out, seen = [], set()
        for m in chosen:
            if m["node_id"] in seen:
                continue
            seen.add(m["node_id"])
            n = self.nodes[m["node_id"]]
            out.append({
                "id": m["node_id"], "name": n["n"], "type": n["t"],
                "type_label": self.type_label(n["t"]), "alias": m["alias"],
                "start": m["start"], "end": m["end"],
            })
            if len(out) >= limit:
                break
        return out

    # -------------------------------------------------- 邻域
    def neighbours(self, nid: str, limit: int = 40) -> list[dict]:
        """把出入边统一成"从当前实体看出去"的视角，前端直接可用。"""
        out = []
        for e in self.out.get(nid, []):
            out.append({"dir": "out", "rel": e["r"], "rel_label": self.rel_label(e["r"]),
                        "other": e["t"], "other_name": self.name(e["t"]),
                        "other_type": self.nodes[e["t"]]["t"], "w": e["w"],
                        "src": e["src"], "u": e.get("u", "")})
        for e in self.inc.get(nid, []):
            out.append({"dir": "in", "rel": e["r"], "rel_label": self.rel_label(e["r"]),
                        "other": e["h"], "other_name": self.name(e["h"]),
                        "other_type": self.nodes[e["h"]]["t"], "w": e["w"],
                        "src": e["src"], "u": e.get("u", "")})
        out.sort(key=lambda x: (-x["w"], x["rel"]))
        return out[:limit]

    def subgraph(self, seeds: list[str], hops: int = 2, limit: int = 70) -> dict:
        seeds = [s for s in seeds if s in self.nodes]
        if not seeds:
            return {"nodes": [], "edges": []}
        dist = {s: 0 for s in seeds}
        order = list(seeds)
        frontier = list(seeds)
        for _ in range(hops):
            nxt = []
            for nid in frontier:
                for e in self.out.get(nid, []) + self.inc.get(nid, []):
                    other = e["t"] if e["h"] == nid else e["h"]
                    if other not in dist:
                        dist[other] = dist[nid] + 1
                        nxt.append(other)
                        order.append(other)
            frontier = nxt
            if len(order) >= limit:
                break
        keep = set(order[:limit])
        nodes = [{
            "id": nid, "n": self.nodes[nid]["n"], "t": self.nodes[nid]["t"],
            "hop": dist[nid], "seed": nid in seeds, "d": self.nodes[nid]["d"],
        } for nid in order if nid in keep]
        edges = [{"h": e["h"], "r": e["r"], "t": e["t"], "w": e["w"],
                  "rl": self.rel_label(e["r"])}
                 for e in self.edges if e["h"] in keep and e["t"] in keep]
        return {"nodes": nodes, "edges": edges}

    # -------------------------------------------------- 前端用的精简视图
    def slim(self) -> dict:
        return {
            "nodes": [{"id": n["id"], "n": n["n"], "t": n["t"], "d": n["d"],
                       "sys": n.get("sys", ""), "c": 1 if n.get("clinic") else 0}
                      for n in self.nodes.values()],
            "edges": [{"h": e["h"], "r": e["r"], "t": e["t"], "w": e["w"]} for e in self.edges],
        }

    def search(self, q: str, limit: int = 12, types: list[str] | None = None) -> list[dict]:
        """实体名搜索：前缀 > 包含 > 别名词，并让高连接度实体靠前。"""
        q = (q or "").strip().lower()
        if not q:
            return []
        scored = []
        for n in self.nodes.values():
            if types and n["t"] not in types:
                continue
            name = n["n"].lower()
            s = 0.0
            if name == q:
                s = 120
            elif name.startswith(q):
                # 名字离查询越近越可能是它，"糖尿病的急性并发症"不该压过"2型糖尿病"
                s = 95 - (len(name) - len(q)) * 4
            elif q in name:
                s = 58
            # 别名无论主名是否命中都要看：「2型糖尿病」的主名里没有前缀，别名「糖尿病」有
            for a in n.get("a", []):
                al = a.lower()
                if al == q:
                    s = max(s, 96)
                elif al.startswith(q):
                    s = max(s, 88 - (len(al) - len(q)) * 4)
                elif q in al:
                    s = max(s, 44)
            if s > 0:
                s += min(28, n["d"]) * 0.9
                scored.append((s, n))
        scored.sort(key=lambda x: -x[0])
        return [{
            "id": n["id"], "name": n["n"], "type": n["t"],
            "type_label": self.type_label(n["t"]), "degree": n["d"],
            "sys": n.get("sys", ""),
        } for _, n in scored[:limit]]


graph = Graph()
