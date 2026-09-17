#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
캐릭터 태그 DB 빌더 — 속성은 사람이 쓰지 않고 **Danbooru 실측으로 채운다** (2026-09-16).

캐릭터 태그가 붙은 글들을 직접 훑어 속성 태그가 몇 % 함께 붙는지 세면, 그게 곧 "모델이 그 태그로
무엇을 학습했는가"입니다. 사람이 애니메이션 화면을 기억해서 속성을 쓰면 틀립니다
(실측: 타이가를 주황머리로 적으면 Danbooru는 1%만 그렇게 봅니다). 그래서 속성은 이 스크립트만 믿습니다.

  산출물: data/chara_tags.yaml   (커밋되는 공용 데이터 — 로컬 경로 없음)
  재현:   venv/bin/python analysis_chara/build.py [--out data/chara_tags.yaml]

  API: /counts/posts.json (총 글 수) + /posts.json?order=fav_count (대표 글 32장) — 캐릭터당 2요청
       대표 글은 "그 캐릭터가 가장 많이 그렇게 그려진다"는 뜻이므로 얼굴 고정 재료로 정확합니다.
"""
import argparse
import collections
import datetime
import json
import os
import sys
import time
import urllib.parse
import urllib.request

COUNTS = "https://danbooru.donmai.us/counts/posts.json?tags={}"
POSTS = "https://danbooru.donmai.us/posts.json?tags={}&limit={}&order=fav_count"
UA = {"User-Agent": "comic-gen/1.0 (character tag DB build)"}
SLEEP = 0.30
SAMPLE = 32

# 측정하는 속성 (DB 키 = Danbooru 태그 원문)
COLORS = {"black_hair", "brown_hair", "blonde_hair", "pink_hair", "red_hair", "blue_hair",
          "purple_hair", "green_hair", "grey_hair", "white_hair", "silver_hair", "orange_hair"}
LENGTH = {"very_short_hair", "short_hair", "bob_cut", "medium_hair", "long_hair", "very_long_hair"}
EYES = {"red_eyes", "blue_eyes", "green_eyes", "brown_eyes", "purple_eyes", "grey_eyes",
        "yellow_eyes", "pink_eyes"}
STYLE = {"twintails", "ponytail", "side_ponytail", "braid", "tri_tails", "bob_cut", "hime_cut",
         "ahoge", "hair_ribbon", "hair_bow", "hair_ornament", "glasses", "wavy_hair", "curly_hair",
         "blunt_bangs", "hair_ponytail", "updo", "bangs"}
OUTFIT = {"school_uniform", "serafuku", "blazer", "dress", "kimono", "yukata", "swimsuit",
          "maid_apron", "business_casual", "suit"}
SKIN = {"dark_skin", "tanned_skin", "pale_skin"}
KID = {"child", "loli", "shota", "tiny"}
ELDER = {"mature_female", "old_woman", "milf"}

# ── 시드: 여성 주인공·미시 후보. 이름은 실측으로 확인된 표제어만 (오타는 'TAG 없음'으로 드러난다) ──
SEED = """
# ① 실사용이 확인된 12작품 계열
shinomiya_kaguya
fujiwara_chika
hayasaka_ai
iino_miko
kitagawa_marin
yamada_anna
aisaka_taiga
kawashima_ami
yuigahama_yui
yukinoshita_yukino
isshiki_iroha
komi_shouko
komi_shuuko
uzaki_hana
uzaki_tsuki
nakano_nino
nakano_ichika
nakano_miku
nakano_yotsuba
nakano_itsuki
kasumigaoka_utaha
katou_megumi
sawamura_spencer_eriri
mizuhara_chizuru
kirisaki_chitoge
onodera_kosaki
futaba_anzu
# ② 속성 사각지대(머리색·눈·나이대)를 메우는 강한 태그들
tohsaka_rin
matou_sakura
tamamo_no_mae_(fate)
artoria_pendragon_(lancer)
ishtar_(fate)
senjougahara_hitagi
hanekawa_tsubasa
makise_kurisu
ayanami_rei
rem_(re:zero)
emilia_(re:zero)
kaname_madoka
akemi_homura
megumin
aqua_(konosuba)
asuna_(sao)
sinon
kamado_nezuko
kugisaki_nobara
power_(chainsaw_man)
makima_(chainsaw_man)
albedo_(overlord)
nagato_yuki
hiiragi_tsukasa
izumi_konata
kinomoto_sakura
tsukino_usagi
shinku
holo
frieren
fern_(sousou_no_frieren)
hoshino_ai
raiden_shogun
yae_miko
hutao
furina
arisu
shiroko_(blue_archive)
gotoh_hitori
shima_rin
anjou_naruko
special_week_(umamusume)
silence_suzuka_(umamusume)
hakurei_reimu
kochiya_sanae
shimakaze_(kancolle)
akagi_(kancolle)
zero_two_(darling_in_the_franxx)
# ③ 미시/MILF·연상 — 검은머리 위주 (mature_female 공반 실측으로 확인됨)
yor_forger
nico_robin
boa_hancock
ada_wong
himeno_(chainsaw_man)
unohana_retsu
ieiri_shoko
nishizumi_shiho
katsuragi_misato
kusanagi_motoko
akagi_ritsuko
hiratsuka_shizuka
trisha_elric
esdeath
shihouin_yoruichi
yuigahama_yui's_mother
# ④ 실측으로 추가 발굴한 흑발 미시 (fav순 mature_female 공반 상위 — 글 수는 작아도 나이대 신호가 강하다)
melony_(pokemon)
delia_ketchum
azuma_fubuki
kali_belladonna
renee_graves
yamada_sanae_(bokuyaba)
cologne_(ranma_1/2)
kuonji_ukyou
usami_renko
maribel_hearn
chun-li
shihouin_yoruichi
wakura_yuuki
tatsuno_saori
tsunade_(naruto)
drasna_(pokemon)
agatha_(pokemon)
integra_hellsing
balalaika_(black_lagoon)
karen_(pokemon)
kalifa_(one_piece)
yamato_(one_piece)
"""


def get(url: str):
    for _ in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=25) as r:
                return json.loads(r.read().decode())
        except Exception:
            time.sleep(1.5)
    return None


def n_of(tags: str) -> int:
    j = get(COUNTS.format(urllib.parse.quote(tags, safe="+:")))
    time.sleep(SLEEP)
    try:
        return int(j["counts"]["posts"])
    except Exception:
        return -1


def ratio(d: dict, key: str, total: int, thresh: int) -> dict:
    return {k: v for k, v in sorted(((k, int(round(100.0 * n / total))) for k, n in d.items()),
                                    key=lambda x: -x[1]) if v >= thresh}


def measure(tag: str) -> dict:
    posts = n_of(tag)
    if posts <= 0:
        return {}
    js = get(POSTS.format(urllib.parse.quote(tag), SAMPLE))
    time.sleep(SLEEP)
    if not isinstance(js, list) or not js:
        return {}
    n = len(js)
    color, length, eyes, style, outfit, skin, kid, elder = (collections.Counter() for _ in range(8))
    ser, rating = collections.Counter(), collections.Counter()
    for d in js:
        ts = set((d.get("tag_string") or "").split()) | set((d.get("tag_string_character") or "").split())
        color.update(ts & COLORS)
        length.update(ts & LENGTH)
        eyes.update(ts & EYES)
        style.update(t for t in ts & STYLE)
        outfit.update(t for t in ts & OUTFIT)
        skin.update(t for t in ts & SKIN)
        if ts & KID:
            kid["child"] += 1
        if ts & ELDER:
            elder["mature"] += 1
        for s in (d.get("tag_string_copyright") or "").split():
            ser[s] += 1
        rating[d.get("rating") or "?"] += 1
    kp = int(round(100.0 * sum(kid.values()) / n))
    mp = int(round(100.0 * sum(elder.values()) / n))
    out = {
        "posts": posts,
        "series": ser.most_common(1)[0][0] if ser else "",
        "sample": n,
        "kid_pct": kp, "mature_pct": mp,
        "age_band": ("child" if kp >= 25 and kp >= mp else "elder" if mp >= 30 else "adult"),
        "explicit_pct": int(round(100.0 * (rating.get("e", 0) + rating.get("q", 0)
                                          + rating.get("explicit", 0) + rating.get("questionable", 0)) / n)),
        "hair_color": ratio(color, "c", n, 20),
        "hair_length": ratio(length, "l", n, 25),
        "eyes": ratio(eyes, "e", n, 25),
        "traits": {k: int(round(100.0 * v / n)) for k, v in style.most_common(8) if v * 100 // n >= 25},
        "outfit": {k: int(round(100.0 * v / n)) for k, v in outfit.most_common(5) if v * 100 // n >= 25},
        "skin": {k: int(round(100.0 * v / n)) for k, v in skin.most_common(3) if v * 100 // n >= 30},
    }
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", default="")
    ap.add_argument("--out", default="data/chara_tags.yaml")
    ap.add_argument("--min-posts", type=int, default=100)
    args = ap.parse_args()

    raw = (open(args.seed, encoding="utf-8").read() if args.seed else SEED.strip())
    tags = [ln.strip() for ln in raw.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    print(f"시드 {len(tags)}개 → Danbooru 실측 (캐릭터당 2요청)")

    db, done = {}, 0
    for t in tags:
        done += 1
        d = measure(t)
        if not d:
            print(f"  [{done:>3}/{len(tags)}] {t:34s} TAG 없음(또는 샘플 없음)")
            continue
        if d["posts"] < args.min_posts:
            print(f"  [{done:>3}/{len(tags)}] {t:34s} {d['posts']:>6} — 최소 글 수 미만, 제외")
            continue
        db[t] = d
        print(f"  [{done:>3}/{len(tags)}] {t:34s} {d['posts']:>6}  hair={list(d['hair_color'])[:2]} "
              f"age={d['age_band']} exp={d['explicit_pct']}% series={d['series'][:28]}")

    def y(d, ind):
        pad = " " * ind
        return "".join(f"{pad}{k}: {v}\n" for k, v in sorted(d.items())) or f"{pad}{{}}\n"

    lines = ["# 캐릭터 태그 DB — Danbooru 실측으로 채운 데이터 (수기 속성 금지)",
             f"# 속성 값 = '그 캐릭터 태그가 붙은 대표 글(fav순 {SAMPLE}장) 중 해당 속성 태그도 붙은 비율(%)'.",
             "#         즉 사람의 기억이 아니라 '모델이 실제로 뭘 많이 봤는가'의 측정치입니다.",
             "# 재생성: venv/bin/python analysis_chara/build.py",
             f"# generated_at: {datetime.date.today().isoformat()}",
             f"# source: danbooru counts/posts.json + posts.json (캐릭터 {len(db)}종)",
             "version: 1",
             "characters:"]
    for t in sorted(db, key=lambda k: -db[k]["posts"]):
        d = db[t]
        lines.append(f"  {t}:")
        lines.append(f"    posts: {d['posts']}")
        lines.append(f'    series: "{d["series"]}"')
        lines.append(f"    age_band: {d['age_band']}")
        lines.append(f"    kid_pct: {d['kid_pct']}")
        lines.append(f"    mature_pct: {d['mature_pct']}")
        lines.append(f"    explicit_pct: {d['explicit_pct']}")
        for k in ("hair_color", "hair_length", "eyes", "traits", "outfit", "skin"):
            lines.append(f"    {k}:")
            lines.append(y(d[k], 6))
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    open(args.out, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    print(f"\n완료: {args.out} ({len(db)}종)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
