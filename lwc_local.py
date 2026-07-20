"""
NTF 컴포넌트 로컬 패치 래퍼.
_lwc_build/ 에 빌드 복사본을 만들고 JS를 두 가지 패치:
  1) visibleLogicalRange 저장/복원 → rerun 후에도 줌/팬 상태 유지
  2) dblclick 이벤트 리스너 → {dblclick: true} 를 Python으로 반환
NTF 패키지 버전이 바뀌면 자동으로 재빌드.
"""
import importlib.metadata
import importlib.util
import os
import shutil

import streamlit.components.v1 as components

_PROJ = os.path.dirname(os.path.abspath(__file__))
_BUILD = os.path.join(_PROJ, "_lwc_build")
_SENTINEL = os.path.join(_BUILD, ".patched")

# ── JS 패치 대상 / 교체 문자열 ────────────────────────────────────────────
_OLD = (
    "f.Streamlit.setComponentValue({time:t.time,prices:i})}})),"
    "c.timeScale().fitContent()"
)
_NEW = (
    # 기존 setComponentValue 호출은 그대로 유지
    "f.Streamlit.setComponentValue({time:t.time,prices:i})}})),"
    # ① 보이는 범위가 바뀔 때마다 window._slwc_r 에 저장
    "c.timeScale().subscribeVisibleLogicalRangeChange((function(r){r&&(window._slwc_r=r)})),"
    # ② 저장된 범위가 있으면 복원, 없으면 fitContent (첫 렌더)
    "(window._slwc_r"
    "?c.timeScale().setVisibleLogicalRange(window._slwc_r)"
    ":c.timeScale().fitContent()),"
    # ③ 컨테이너에 dblclick 리스너 1회 등록 (_slwc_dbl 플래그로 중복 방지)
    "(function(){"
    'var _el=document.getElementById("lightweight-charts-0");'
    "_el&&!_el._slwc_dbl&&(_el._slwc_dbl=1,"
    '_el.addEventListener("dblclick",'
    "(function(){f.Streamlit.setComponentValue({dblclick:!0})})))"
    "})()"
)


def _sentinel_tag():
    try:
        ver = importlib.metadata.version("streamlit-lightweight-charts-ntf")
    except Exception:
        ver = "unknown"
    return f"patched-v1-{ver}"


def _needs_rebuild():
    if not os.path.exists(_SENTINEL):
        return True
    try:
        with open(_SENTINEL, encoding="utf-8") as f:
            return f.read().strip() != _sentinel_tag()
    except Exception:
        return True


def _build():
    spec = importlib.util.find_spec("streamlit_lightweight_charts_ntf")
    if spec is None:
        raise ImportError("streamlit-lightweight-charts-ntf 패키지가 설치되지 않았습니다.")
    ntf_build = os.path.join(os.path.dirname(spec.origin), "frontend", "build")

    if os.path.exists(_BUILD):
        shutil.rmtree(_BUILD)
    shutil.copytree(ntf_build, _BUILD)

    # 활성 main.*.chunk.js 에서 패턴을 찾아 교체
    js_dir = os.path.join(_BUILD, "static", "js")
    patched = False
    for fname in sorted(os.listdir(js_dir)):
        if not (fname.startswith("main.") and fname.endswith(".chunk.js")):
            continue
        path = os.path.join(js_dir, fname)
        with open(path, encoding="utf-8") as f:
            src = f.read()
        if _OLD not in src:
            continue
        with open(path, "w", encoding="utf-8") as f:
            f.write(src.replace(_OLD, _NEW, 1))
        patched = True
        break

    if not patched:
        raise RuntimeError(
            "_lwc_build JS 패치 실패: 대상 패턴을 찾지 못했습니다. "
            "NTF 패키지 버전을 확인하세요."
        )

    with open(_SENTINEL, "w", encoding="utf-8") as f:
        f.write(_sentinel_tag())


# ── 빌드 (필요한 경우) ──────────────────────────────────────────────────
if _needs_rebuild():
    _build()

_component_func = components.declare_component(
    "streamlit_lightweight_charts",
    path=_BUILD,
)


def renderLightweightCharts(charts, key=None):
    return _component_func(charts=charts, key=key)
