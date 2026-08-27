# -*- coding: utf-8 -*-
"""★ 報告保留政策（2026-08-27 新增，使用者指示）：
   REPO 只保留最新 10 個開盤日的 YYYYMMDD 報告資料夾，其餘刪除。

   跑「今日／昨日報告」前先執行本腳本（README 每日流程第 0.5 步、守則第 12 節）：
   檢查有無 10 期以外的舊資料夾，有就先刪除再跑報告。
   - 刪除的期別仍在 git 歷史中，可用 git 回溯；刪除由使用者下一次 commit 一併入版。
   - finalize.py 重建首頁時以現存資料夾為準，清完自然只列 10 期。
"""
import os, re, shutil, sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import config as C

KEEP = 10

dirs = sorted(d for d in os.listdir(C.REPO)
              if re.fullmatch(r"\d{8}", d) and os.path.isdir(os.path.join(C.REPO, d)))
# 視窗包含「即將產出的基準日」（config.YMD）：跑新一天的報告前，
# 舊的第 10 期就已在 10 期視窗之外，先刪再跑，跑完恰好 10 期
universe = sorted(set(dirs) | {C.YMD})
keep_set = set(universe[-KEEP:])
old = [d for d in dirs if d not in keep_set]

for d in old:
    shutil.rmtree(os.path.join(C.REPO, d))
    print("刪除", d)

kept = [d for d in dirs if d in keep_set]
if kept:
    print("保留 %d 期：%s ~ %s（本次基準日 %s 產出後共 %d 期）"
          % (len(kept), kept[0], kept[-1], C.YMD, len(set(kept) | {C.YMD})))
if old:
    print("★ 已刪除 %d 期（%s）——歷史仍在 git 紀錄中可回溯，"
          "刪除將於下次 commit 一併入版（GitHub Pages 舊連結將失效）" % (len(old), "、".join(old)))
else:
    print("無超過保留期限（%d 期）的資料夾，不需清理" % KEEP)
