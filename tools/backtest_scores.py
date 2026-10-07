# -*- coding: utf-8 -*-
"""分數權重回測（2026-10-06 建立；結論與改制見守則 §9、§9.0A、§9.1、§18）

用法（在 tools/ 底下）：
  python backtest_scores.py fetch   # 增量補抓三大法人日資料 → data/inst_history.json
  python backtest_scores.py         # 跑 A／B／C 三段回測並印出結果

前置：先跑過 fetch_quotes.py（data/raw/ 要有近兩年行情）；報告期分數取自 git 歷史的 YYYYMMDD/index.html。
只讀報告與 inputs/，除了 fetch 會寫 data/inst_history.json 之外不寫任何檔。

  A. 報告期：五面向／綜合分 vs 後續超額報酬（逐日橫斷面 Rank IC）、分數落點與雙情境標籤之後的報酬
  B. 一年技術面：判讀分的基礎因子、tech_adj 各事件、RS（用 calc_indicators.analyze 逐日重算）
  C. 一年三大法人：各因子全年與分期 IC、事件之後的報酬

判讀注意：
  - 超額報酬＝個股還原報酬 − 加權指數報酬；「相對同組」再扣掉同日各檔平均。
  - 5／10 日報酬窗互相重疊，t 值已依重疊粗略縮小；8 檔的橫斷面很窄，|IC| < 0.05 視為沒有訊號。
  - ★ 先看分期是否一致再下結論：2026-08/09 是反常盤勢（價格與法人因子一起轉負），只用一段期間的結果不要改規則。
"""
import json, os, re, sys, time, datetime, subprocess
import numpy as np

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
sys.stdout.reconfigure(encoding="utf-8")
import config as C
import lib

RAW = os.path.join(BASE, "data", "raw")
INST_P = os.path.join(BASE, "data", "inst_history.json")
TZ = datetime.timezone(datetime.timedelta(hours=8))
LAST = C.YMD                      # 只用到基準日為止（濾除盤中未完成 K 棒）
H = (1, 3, 5, 10)
REPORT_START = "20260807"         # 報告期：git 歷史中有五面向分數的第一期

# 追蹤標的＋已移除但仍在歷史報告中的個股（代號: (Yahoo 代碼, 市場)）
SYM = {c: (y, m) for c, _, y, m, _ in C.STOCKS}
REMOVED = {"5371": ("5371.TWO", "上櫃")}   # 08/24 起移出（守則第 1 節）
for c, v in REMOVED.items():
    if os.path.exists(os.path.join(RAW, v[0] + ".json")):
        SYM.setdefault(c, v)
# 面額變更：此日前證交所 T86 為舊股數 → 乘倍數換成新股數（Yahoo 量價已還原）
SHARE_SPLIT = {"8422": ("20251117", 10)}


def load_raw(sym):
    d = json.load(open(os.path.join(RAW, sym.replace("^", "IDX_") + ".json")))["chart"]["result"][0]
    ind = d["indicators"]
    c = (ind.get("adjclose") or [{}])[0].get("adjclose") or ind["quote"][0]["close"]
    px, vol = {}, {}
    for t, v, q in zip(d["timestamp"], c, ind["quote"][0]["volume"]):
        ds = datetime.datetime.fromtimestamp(t, TZ).strftime("%Y%m%d")
        if v is not None and ds <= LAST:
            px[ds], vol[ds] = v, q or 0
    return px, vol


IDX, _ = load_raw(C.INDEX_SYM)
DAYS = sorted(IDX)
DI = {d: i for i, d in enumerate(DAYS)}
PX, VOL = {}, {}
for c, (y, _) in SYM.items():
    PX[c], VOL[c] = load_raw(y)


def fwd(c, d, h):
    """d 收盤起 h 個交易日的超額報酬；資料不足回 None"""
    i = DI.get(d)
    if i is None or i + h >= len(DAYS) or d not in PX[c] or DAYS[i + h] not in PX[c]:
        return None
    d1 = DAYS[i + h]
    return PX[c][d1] / PX[c][d] - IDX[d1] / IDX[d]


def past_ex(c, d, n):
    i = DI.get(d)
    if i is None or i < n or d not in PX[c] or DAYS[i - n] not in PX[c]:
        return None
    d0 = DAYS[i - n]
    return (PX[c][d] / PX[c][d0] - IDX[d] / IDX[d0]) * 100


def add_fwd(rows):
    """補上 x1..x10（超額）、r1..r10（絕對）與 g1..g10（相對同組）"""
    for r in rows:
        for h in H:
            x = fwd(r["code"], r["date"], h)
            if x is not None:
                r["x%d" % h] = x
                i = DI[r["date"]]
                r["r%d" % h] = PX[r["code"]][DAYS[i + h]] / PX[r["code"]][r["date"]] - 1
    by = {}
    for r in rows:
        by.setdefault(r["date"], []).append(r)
    for g in by.values():
        for h in H:
            k = "x%d" % h
            v = [z[k] for z in g if k in z]
            if len(v) >= 5:
                m = np.mean(v)
                for z in g:
                    if k in z:
                        z["g%d" % h] = z[k] - m
    return rows


def rank(a):
    a = np.asarray(a, float)
    o = a.argsort(kind="mergesort")
    r = np.empty(len(a))
    r[o] = np.arange(len(a))
    for v in np.unique(a):
        m = a == v
        r[m] = r[m].mean()
    return r


def csic(rows, fx, h):
    """逐日橫斷面 Rank IC：回傳 (平均, 為正比例, 依重疊縮小後的 t)"""
    key, by = "x%d" % h, {}
    for r in rows:
        if key in r and fx(r) is not None:
            by.setdefault(r["date"], []).append(r)
    v = []
    for g in by.values():
        x = [fx(z) for z in g]
        if len(g) >= 5 and np.std(x) > 0:
            v.append(np.corrcoef(rank(x), rank([z[key] for z in g]))[0, 1])
    if len(v) < 3:
        return np.nan, np.nan, np.nan
    v = np.array(v)
    return v.mean(), (v > 0).mean(), v.mean() / (v.std(ddof=1) / np.sqrt(max(2, len(v) / h)))


def ic_line(name, rows, fx, w=22):
    return ("  %-" + str(w) + "s") % name + "".join(
        "  %2d日 %+.3f(勝%3.0f%% t%+.1f)" % ((h,) + (lambda a: (a[0], a[1] * 100, a[2]))(csic(rows, fx, h))) for h in H)


def ev_line(name, rows, cond, key="g", w=30):
    g = [r for r in rows if cond(r)]
    s = ("  %-" + str(w) + "s n=%4d") % (name, len(g))
    for h in H:
        v = np.array([r["%s%d" % (key, h)] for r in g if "%s%d" % (key, h) in r])
        if len(v) > 2:
            t = v.mean() / (v.std(ddof=1) / np.sqrt(max(2, len(v) / h)))
            s += " | %2d日 %+5.2f%%(t%+.1f)" % (h, v.mean() * 100, t)
    return s


# ── A. 報告期：從 git 歷史抽五面向分數 ─────────────────────────────
FACETS = ["籌碼面", "技術面", "基本面", "大盤面", "消息面"]


def git(*a):
    return subprocess.run(["git", "-C", C.REPO, *a], capture_output=True).stdout.decode("utf-8", "replace")


def report_rows():
    paths = sorted({l for l in git("log", "--format=", "--name-only", "--", "2026*/index.html").split("\n")
                    if re.match(r"^\d{8}/index.html$", l)})
    rows = []
    for p in paths:
        if p[:8] > LAST:
            continue
        h = git("log", "--diff-filter=AM", "-1", "--format=%h", "--", p).strip()
        html = git("show", "%s:%s" % (h, p))
        m = re.search(r'<span class="mcl">大盤環境分</span><span class="mcv">(\d+)', html)
        env = int(m.group(1)) if m else None
        for m in re.finditer(r'<section id="s(\d{4})"(.*?)</section>', html, re.S):
            code, body = m.group(1), m.group(2)
            sc = {}
            for f in FACETS:
                mm = re.search(re.escape(f) + r'<em>[^<]*</em></span>.*?<span class="sbv">(\d+)</span>', body, re.S)
                if mm:
                    sc[f] = int(mm.group(1))
            mt = re.search(r'綜合分數</span>.*?<span class="sbv">(\d+)</span>', body, re.S)
            if len(sc) < 5 or not mt or code not in SYM:
                continue
            mj = re.search(r'判讀分 (\d+) ', body)
            r = dict(date=p[:8], code=code, chip=sc["籌碼面"], tech=sc["技術面"], fund=sc["基本面"],
                     mkt=sc["大盤面"], news=sc["消息面"], total=int(mt.group(1)), env=env,
                     judge=int(mj.group(1)) if mj else sc["技術面"])
            for side, key in (("空手時", "e_lab"), ("已持有", "h_lab")):
                ml = re.search(r'<span class="actw">%s</span>.*?<span class="chip [a-z\-]+[^"]*"><i>[^<]*</i>([^<]+)</span>'
                               % side, body, re.S)
                r[key] = ml.group(1) if ml else None
            mf = re.search(r'內部拆解：' + "、".join(re.escape(q) + r' (\d+)/\d+' for q, _ in C.FUND_PARTS), body)
            if mf:
                for (q, _), v in zip(C.FUND_PARTS, mf.groups()):
                    r["F_" + q] = int(v)
            rows.append(r)
    return add_fwd(rows)


def section_a():
    R = report_rows()
    print("\n" + "=" * 100)
    print("A. 報告期（%s ~ %s，%d 期、%d 筆）：分數 vs 之後的超額報酬" % (
        min(r["date"] for r in R), max(r["date"] for r in R), len({r["date"] for r in R}), len(R)))
    print("   ⚠ 各期分數依當時的權重與規則產生（2026-10-06 前為 30/20/20/20/10、大盤面含 RS）")
    print("=" * 100)
    print("\n[A1] 五面向與綜合分：逐日橫斷面 Rank IC（正＝分數高者之後較強）")
    for nm, k in (("籌碼", "chip"), ("技術（含加減分）", "tech"), ("技術判讀分", "judge"), ("基本", "fund"),
                  ("大盤面", "mkt"), ("消息", "news"), ("綜合分", "total")):
        print(ic_line(nm, R, lambda r, k=k: r[k]))
    print("\n[A2] 基本面內部五項")
    for q, _ in C.FUND_PARTS:
        print(ic_line(q, R, lambda r, q=q: r.get("F_" + q)))
    print("\n[A3] 綜合分落點之後的絕對報酬")
    for lo, hi, nm in ((70, 101, "≥70 買進"), (55, 70, "55–69 觀察偏多"), (45, 55, "45–54 觀察"), (0, 45, "<45 減碼")):
        print(ev_line(nm, R, lambda r, lo=lo, hi=hi: lo <= r["total"] < hi, key="r"))
    print("\n[A4] 雙情境標籤之後的絕對報酬")
    for key, side in (("e_lab", "空手"), ("h_lab", "持有")):
        for L in sorted({r[key] for r in R if r[key]}):
            print(ev_line("%s %s" % (side, L), R, lambda r, L=L, key=key: r[key] == L, key="r"))


# ── B. 一年技術面 ──────────────────────────────────────────────────
def tech_rows():
    src = open(os.path.join(BASE, "calc_indicators.py"), encoding="utf-8").read().split("idx_rows = load(")[0]
    g = {"__file__": os.path.join(BASE, "calc_indicators.py"), "__name__": "calc_indicators_bt"}
    exec(src, g)
    out = []
    for c, (y, _) in SYM.items():
        rows = g["load"](y)
        for i in range(250, len(rows)):
            a = g["analyze"](rows[:i + 1], c)
            if not a["vr"] or not a["vr20"] or a["bias20"] is None or a["pb"] is None:
                continue                      # 停牌／零量日（例：8422 面額變更）
            ev = {}
            for side in ("top", "bottom"):
                hh = (a.get("macd_div") or {}).get(side)
                if hh and hh["bars_since"] <= lib.DIV_HALF_BARS:
                    ev[hh["kind"]] = 1
            for k in ("3-6", "6-12", "5-20"):
                d = (a.get("dma") or {}).get(k)
                if d and d["cross"] != "無":
                    ev["DMA %s %s" % (k, d["cross"])] = 1
            if a["break_up"]:
                ev["突破前20日高"] = 1
            if a["break_dn"]:
                ev["跌破前20日低"] = 1
            gt = (a.get("gaps") or {}).get("today")
            if gt:
                ev["向上跳空" if gt == "up" else "向下跳空"] = 1
            ds = a["date"].replace("-", "")
            ex = [past_ex(c, ds, n) for n in (5, 20, 60)]
            out.append(dict(code=c, date=ds, above=sum(1 for k in ("5", "10", "20", "60", "120", "240")
                                                       if a["close"] > a["ma"][k]),
                            ref=lib.tech_anchor(a)[2], adj=lib.tech_adj(a)[0], bias20=a["bias20"], rsi=a["rsi"],
                            pb=a["pb"], k=a["k"], vr20=a["vr20"], from_hi52=a["from_hi52"], ev=ev,
                            rs=None if None in ex else lib.rs_score(*ex)))
    return add_fwd(out)


def section_b():
    T = tech_rows()
    print("\n" + "=" * 100)
    print("B. 一年技術面（%s ~ %s，%d 筆）" % (min(r["date"] for r in T), max(r["date"] for r in T), len(T)))
    print("=" * 100)
    print("\n[B1] 判讀分的基礎因子與 RS：逐日橫斷面 Rank IC")
    for nm, k in (("站上均線條數", "above"), ("錨定參考落點", "ref"), ("月線乖離", "bias20"), ("RSI", "rsi"),
                  ("%B", "pb"), ("K 值", "k"), ("量比 vr20", "vr20"), ("距 52 週高", "from_hi52"),
                  ("RS 分（大盤面已不計）", "rs"), ("現行 tech_adj 合計", "adj")):
        print(ic_line(nm, T, lambda r, k=k: r[k]))
    print("\n[B2] tech_adj 各事件之後的相對同組超額報酬（括號為現行給分）")
    cur = {"頂背離": lib.DIV_ADJ["頂背離"], "底背離": lib.DIV_ADJ["底背離"], "隱性頂背離": lib.DIV_ADJ["隱性頂背離"],
           "隱性底背離": lib.DIV_ADJ["隱性底背離"], "突破前20日高": lib.BRK_UP_ADJ, "跌破前20日低": -lib.BRK_DN_ADJ,
           "向上跳空": lib.GAP_UP_ADJ, "向下跳空": -lib.GAP_DN_ADJ}
    for k in ("3-6", "6-12", "5-20"):
        cur["DMA %s 黃金交叉" % k], cur["DMA %s 死亡交叉" % k] = lib.DMA_CROSS_ADJ, -lib.DMA_CROSS_ADJ
    for k, w in cur.items():
        print(ev_line("%s（%+g）" % (k, w or 0), T, lambda r, k=k: k in r["ev"]))
    print("\n[B3] 過熱／超跌狀態之後的相對同組超額報酬")
    for nm, cond in (("月線乖離 > 8%", lambda r: r["bias20"] > 8), ("%B > 100（上軌外）", lambda r: r["pb"] > 100),
                     ("K > 80", lambda r: r["k"] > 80), ("站上 6/6", lambda r: r["above"] == 6),
                     ("站上 3/6", lambda r: r["above"] == 3), ("月線乖離 < −8%", lambda r: r["bias20"] < -8)):
        print(ev_line(nm, T, cond))
    return T


# ── C. 一年三大法人 ────────────────────────────────────────────────
def inst_rows():
    if not os.path.exists(INST_P):
        return []
    D = json.load(open(INST_P, encoding="utf-8"))["data"]
    out = []
    for c, m in D.items():
        if c not in SYM:
            continue
        ds_all = sorted(d for d in m if d in PX[c] and d in DI)
        vd = sorted(d for d in VOL[c] if VOL[c][d] > 0)
        for i, d in enumerate(ds_all):
            if i < 20:
                continue
            w = [m[x] for x in ds_all[i - 19:i + 1]][::-1]          # 新→舊；欄位見 inst_history.json _note
            v20 = np.mean([VOL[c][x] for x in [y for y in vd if y <= d][-20:]])
            if not v20:
                continue
            s = lambda j, n: sum(x[j] for x in w[:n]) / v20
            r = dict(code=c, date=d, f1=s(0, 1), f5=s(0, 5), f10=s(0, 10), f20=s(0, 20), i5=s(2, 5), i10=s(2, 10),
                     d5=s(3, 5), t5=s(4, 5), t10=s(4, 10), past10=past_ex(c, d, 10))
            for j, nm in ((0, "fst"), (2, "ist")):                     # 連買為正、連賣為負
                st = 0
                for x in w:
                    if x[j] > 0 and st >= 0:
                        st += 1
                    elif x[j] < 0 and st <= 0:
                        st -= 1
                    else:
                        break
                r[nm] = st
            out.append(r)
    return add_fwd(out)


PERIODS = [("2025/09~12", "20250901", "20251231"), ("2026/01~03", "20260101", "20260331"),
           ("2026/04~06", "20260401", "20260630"), ("2026/07~報告期前", "20260701", "20260806"),
           ("報告期", REPORT_START, "20991231")]


def section_c(T):
    I = inst_rows()
    print("\n" + "=" * 100)
    if not I:
        print("C. 三大法人：找不到 data/inst_history.json，先跑 python backtest_scores.py fetch")
        return
    print("C. 一年三大法人（%s ~ %s，%d 筆；買超皆除以 20 日均量）" % (
        min(r["date"] for r in I), max(r["date"] for r in I), len(I)))
    print("=" * 100)
    F = (("外資連買賣天數", "fst"), ("外資 1 日", "f1"), ("外資 5 日", "f5"), ("外資 10 日", "f10"), ("外資 20 日", "f20"),
         ("投信連買賣天數", "ist"), ("投信 5 日", "i5"), ("投信 10 日", "i10"), ("自營 5 日", "d5"),
         ("三大 5 日", "t5"), ("三大 10 日", "t10"))
    print("\n[C1] 全年：逐日橫斷面 Rank IC")
    for nm, k in F:
        print(ic_line(nm, I, lambda r, k=k: r[k]))
    print("\n[C2] 分期 5 日 IC（★ 分期方向一致才算穩定）")
    TT = {(r["date"], r["code"]): r for r in T}
    rows_p = [("外資連買賣天數", I, lambda r: r["fst"]), ("外資 5 日", I, lambda r: r["f5"]),
              ("三大 5 日", I, lambda r: r["t5"]), ("投信 10 日", I, lambda r: r["i10"]),
              ("技術：站上均線條數", I, lambda r: (TT.get((r["date"], r["code"])) or {}).get("above")),
              ("技術：月線乖離", I, lambda r: (TT.get((r["date"], r["code"])) or {}).get("bias20")),
              ("RS（近 10 日超額）", I, lambda r: r["past10"])]
    print("  %-20s" % "" + "".join("%18s" % p[0] for p in PERIODS))
    for nm, rs, fx in rows_p:
        print("  %-20s" % nm + "".join("%18s" % ("%+.3f" % csic([r for r in rs if a <= r["date"] <= b], fx, 5)[0])
                                       for _, a, b in PERIODS))
    print("\n[C3] 事件之後的相對同組超額報酬（全年）")
    for nm, cond in (("外資連買 ≥3 日", lambda r: r["fst"] >= 3), ("外資連買 ≥5 日", lambda r: r["fst"] >= 5),
                     ("外資連賣 ≥3 日", lambda r: r["fst"] <= -3), ("投信連買 ≥3 日", lambda r: r["ist"] >= 3),
                     ("投信連賣 ≥3 日", lambda r: r["ist"] <= -3), ("外資 5 日買超 > 均量 50%", lambda r: r["f5"] > .5),
                     ("外資 5 日賣超 > 均量 50%", lambda r: r["f5"] < -.5),
                     ("土洋同買（外資、投信 5 日皆買）", lambda r: r["f5"] > 0 and r["i5"] > 0),
                     ("土洋同賣", lambda r: r["f5"] < 0 and r["i5"] < 0)):
        print(ev_line(nm, I, cond))


# ── fetch：增量補抓三大法人 ─────────────────────────────────────────
def fetch(start="20250815"):
    import requests
    hdr = {"User-Agent": "Mozilla/5.0"}
    db = json.load(open(INST_P, encoding="utf-8")) if os.path.exists(INST_P) else {"data": {}, "_empty": []}
    empty = set(db.get("_empty", []))
    have = set()
    for m in db["data"].values():
        have |= set(m)
    todo = [d for d in DAYS if d >= start and d not in have and d not in empty]
    print("待補 %d 個交易日" % len(todo))
    twse = {c for c, (_, mk) in SYM.items() if mk == "上市"}
    tpex = {c for c, (_, mk) in SYM.items() if mk == "上櫃"}

    def get(url, params):
        for k in range(5):
            try:
                r = requests.get(url, params=params, headers=hdr, timeout=30)
                if r.status_code == 200 and r.text.strip().startswith("{"):
                    return r.json()
            except Exception as e:
                print("   retry", k, e)
            time.sleep(15 * (k + 1))
        return None

    for n, ds in enumerate(todo, 1):
        got = 0
        j = get("https://www.twse.com.tw/rwd/zh/fund/T86", {"date": ds, "selectType": "ALLBUT0999", "response": "json"})
        for row in (j or {}).get("data", []):
            c = row[0].strip()
            if c in twse:
                v = [int(x.replace(",", "")) for x in row[2:]]
                k = SHARE_SPLIT[c][1] if c in SHARE_SPLIT and ds < SHARE_SPLIT[c][0] else 1
                db["data"].setdefault(c, {})[ds] = [x * k for x in (v[2] + v[5], v[2], v[8], v[9], v[16])]
                got += 1
        time.sleep(3.5)                                   # 證交所限流：太密會被暫時封鎖
        j2 = get("https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade",
                 {"type": "Daily", "sect": "EW", "date": "%s/%s/%s" % (ds[:4], ds[4:6], ds[6:]), "response": "json"})
        for row in ((j2 or {}).get("tables") or [{}])[0].get("data", []):
            c = row[0].strip()
            if c in tpex:
                v = [int(x.replace(",", "")) for x in row[2:]]
                db["data"].setdefault(c, {})[ds] = [v[8], v[2], v[11], v[20], v[21]]
                got += 1
        time.sleep(1.5)
        if j and j2 and not got:
            empty.add(ds)                                 # 兩邊都回應但無資料（例：颱風停市）
        if n % 20 == 0 or n == len(todo):
            db["_empty"] = sorted(empty)
            db["data"] = {c: dict(sorted(m.items())) for c, m in sorted(db["data"].items())}
            json.dump(db, open(INST_P, "w", encoding="utf-8"), ensure_ascii=False, separators=(",", ":"))
            print("   %s 已存（%d/%d）" % (ds, n, len(todo)))


if __name__ == "__main__":
    if sys.argv[1:] == ["fetch"]:
        fetch()
    else:
        print("回測基準日 %s；超額報酬以 %s 為基準" % (C.BASE_DATE, C.INDEX_NAME))
        section_a()
        T = section_b()
        section_c(T)
