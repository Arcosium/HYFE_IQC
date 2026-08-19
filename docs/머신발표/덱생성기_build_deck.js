// GenomicWQB 머신 발표 덱 v2.2 — 16장(Q&A 포함), 스토리텔링 구성 (2026-08-19)
// v2.2: 대시보드 실화면 스크린샷 삽입(관제·진화분석·채점·심사기준) + 심사 기준 5종 매핑 장 추가
// 종이/잉크 원장 테마 (대시보드·구버전 덱과 동일 계열)
// 스크린샷 실체는 vault (계정 이메일 포함 — projects 업로드 금지)
const pptxgen = require("pptxgenjs");

const SHOT = "/home/arcosium/vault/GenomicWQB-docs/docs/머신발표/screenshots/";

const C = {
  paper: "F5F1E8",
  panel: "ECE6D8",
  ink: "1C1914",
  body: "45413A",
  mute: "8C8579",
  rule: "D9D2C4",
  accent: "A63A22",
  paperOnDark: "F5F1E8",
  muteOnDark: "B8B0A2",
};
const F = {
  serif: "Noto Serif CJK KR",
  sans: "Noto Sans CJK KR",
  mono: "Noto Sans Mono CJK KR",
};
const W = 13.33, H = 7.5;
const MX = 0.75; // left/right margin
const CW = W - MX * 2; // content width

const pptx = new pptxgen();
pptx.defineLayout({ name: "WIDE", width: W, height: H });
pptx.layout = "WIDE";
pptx.author = "김현호";
pptx.title = "GenomicWQB 머신 발표";

const FOOTER = "GenomicWQB  ·  2026 컨설턴트 서머 부트캠프 머신 발표회";

function base(kickerNo, kickerLabel, pageNo) {
  const s = pptx.addSlide();
  s.background = { color: C.paper };
  // kicker
  s.addText([
    { text: kickerNo, options: { fontFace: F.mono, bold: true, color: C.ink } },
    { text: "  ·  " + kickerLabel, options: { fontFace: F.sans, color: C.mute } },
  ], { x: MX, y: 0.42, w: 6, h: 0.3, fontSize: 10.5, align: "left" });
  // page footer
  s.addText(FOOTER, { x: MX, y: H - 0.48, w: 8, h: 0.28, fontSize: 8, fontFace: F.sans, color: C.mute, align: "left" });
  s.addText(String(pageNo), { x: W - MX - 1, y: H - 0.48, w: 1, h: 0.28, fontSize: 9, fontFace: F.mono, color: C.mute, align: "right" });
  return s;
}

function title(s, text, sub) {
  s.addText(text, { x: MX, y: 0.78, w: CW, h: 0.62, fontSize: 24, fontFace: F.serif, bold: true, color: C.ink, align: "left" });
  if (sub) s.addText(sub, { x: MX, y: 1.42, w: CW, h: 0.5, fontSize: 12.5, fontFace: F.sans, color: C.body, align: "left" });
  // hairline
  s.addShape(pptx.ShapeType.line, { x: MX, y: sub ? 1.98 : 1.52, w: CW, h: 0, line: { color: C.rule, width: 1 } });
}

function panel(s, x, y, w, h, fill) {
  s.addShape(pptx.ShapeType.rect, { x, y, w, h, fill: { color: fill || C.panel }, line: { type: "none" } });
}

// 대시보드 스크린샷 — 패널 매트 위에 얹고 캡션을 단다
function shot(s, file, x, y, w, h, cap) {
  panel(s, x - 0.06, y - 0.06, w + 0.12, h + 0.12);
  s.addImage({ path: SHOT + file, x, y, w, h });
  if (cap) s.addText(cap, { x, y: y + h + 0.12, w, h: 0.3, fontSize: 9.5, fontFace: F.sans, color: C.mute, align: "left" });
}

// ───────────────────────────── 1. 표지
{
  const s = pptx.addSlide();
  s.background = { color: C.ink };
  s.addText("2026 컨설턴트 서머 부트캠프  ·  머신 발표회", {
    x: MX, y: 1.05, w: CW, h: 0.35, fontSize: 12, fontFace: F.sans, color: C.muteOnDark, align: "left",
  });
  s.addText("GenomicWQB", {
    x: MX, y: 1.75, w: CW, h: 1.3, fontSize: 60, fontFace: F.serif, bold: true, color: C.paperOnDark, align: "left",
  });
  s.addShape(pptx.ShapeType.line, { x: MX + 0.03, y: 3.18, w: 2.2, h: 0, line: { color: C.accent, width: 2.5 } });
  s.addText([
    { text: "알파를 유전자로 쪼개서 진화시키는", options: { breakLine: true } },
    { text: "자동 알파 발굴 머신", options: {} },
  ], { x: MX, y: 3.45, w: CW, h: 1.0, fontSize: 21, fontFace: F.serif, color: C.paperOnDark, align: "left", lineSpacingMultiple: 1.25 });
  s.addText("103일  ·  시뮬레이션 38,431건  ·  1,138라운드  ·  제출 93건", {
    x: MX, y: 4.85, w: CW, h: 0.35, fontSize: 13, fontFace: F.mono, color: C.muteOnDark, align: "left",
  });
  s.addText([
    { text: "김현호", options: { bold: true, color: C.paperOnDark } },
    { text: "      2026. 08. 19  ·  WorldQuant BRAIN", options: { color: C.muteOnDark } },
  ], { x: MX, y: 6.55, w: CW, h: 0.35, fontSize: 12, fontFace: F.sans, align: "left" });
}

// ───────────────────────────── 2. IQC 시절
{
  const s = base("01", "출발점 — IQC 시절", 2);
  title(s, "처음엔 브라우저를 조종하는 로봇이었습니다",
    "IQC 때의 1세대 머신은 Playwright 자동화였습니다. 아이디와 비밀번호만 넣으면 사람 대신 웹 화면을 눌렀습니다.");

  // 좌: 4단계 흐름
  const steps = [
    ["로그인", "Playwright가 브라우저를 열고 BRAIN에 대신 로그인합니다."],
    ["생성", "LLM이 Fast Expression을 만들어 시뮬레이션 창에 입력합니다."],
    ["판정", "결과 화면을 읽어 Submit이 가능한지 확인합니다."],
    ["분류", "가능하면 제출 리스트에 담고 Fail이 뜨면 그냥 버립니다."],
  ];
  let y = 2.35;
  steps.forEach((st, i) => {
    s.addText(String(i + 1), { x: MX, y: y, w: 0.45, h: 0.42, fontSize: 17, fontFace: F.mono, bold: true, color: C.accent, align: "left" });
    s.addText([
      { text: st[0] + "   ", options: { bold: true, color: C.ink, fontFace: F.serif, fontSize: 14 } },
      { text: st[1], options: { color: C.body, fontFace: F.sans, fontSize: 12 } },
    ], { x: MX + 0.55, y: y, w: 6.6, h: 0.5, align: "left" });
    if (i < 3) s.addText("↓", { x: MX + 0.02, y: y + 0.44, w: 0.4, h: 0.35, fontSize: 13, color: C.mute, align: "left" });
    y += 0.92;
  });

  // 우: 성과와 한계
  const rx = 8.35, rw = W - MX - rx;
  s.addText("혼자 찾아 제출한 알파", { x: rx, y: 2.3, w: rw, h: 0.3, fontSize: 11, fontFace: F.sans, color: C.mute, align: "left" });
  s.addText([
    { text: "16", options: { fontSize: 58, fontFace: F.serif, bold: true, color: C.ink } },
    { text: " 건", options: { fontSize: 18, fontFace: F.serif, color: C.body } },
  ], { x: rx, y: 2.55, w: rw, h: 1.05, align: "left" });
  panel(s, rx, 3.85, rw, 2.15);
  s.addShape(pptx.ShapeType.line, { x: rx, y: 3.85, w: 0, h: 2.15, line: { color: C.accent, width: 2.5 } });
  s.addText([
    { text: "그런데 점수가 나빴습니다", options: { bold: true, fontFace: F.serif, fontSize: 13.5, color: C.ink, breakLine: true } },
    { text: "", options: { fontSize: 5, breakLine: true } },
    { text: "LLM이 뽑는 로직이 뻔해서 알파들이 서로", options: { fontSize: 12, color: C.body, breakLine: true } },
    { text: "너무 닮았습니다. self-correlation이 높으면", options: { fontSize: 12, color: C.body, breakLine: true } },
    { text: "몇 건을 내도 좋은 평가를 받지 못합니다.", options: { fontSize: 12, color: C.body } },
  ], { x: rx + 0.25, y: 4.05, w: rw - 0.45, h: 1.8, fontFace: F.sans, align: "left", lineSpacingMultiple: 1.28 });
  s.addText("숙제 : 양이 아니라 “서로 다른 알파”를 찾는 기계가 필요했습니다.", {
    x: MX, y: 6.35, w: CW, h: 0.35, fontSize: 12.5, fontFace: F.serif, bold: true, color: C.accent, align: "left",
  });
}

// ───────────────────────────── 3. 아이디어 — 공포·탐욕 지수
{
  const s = base("02", "아이디어의 출처", 3);
  title(s, "실패했던 옛 연구가 힌트를 줬습니다",
    "컨설턴트가 된 뒤, 예전에 유전 알고리즘을 썼던 연구 하나가 떠올랐습니다.");

  // 좌 카드: 옛 연구
  const lw = 5.5;
  panel(s, MX, 2.35, lw, 3.5);
  s.addText("한국 주식시장 공포·탐욕 지수", { x: MX + 0.3, y: 2.62, w: lw - 0.6, h: 0.35, fontSize: 15, fontFace: F.serif, bold: true, color: C.ink, align: "left" });
  s.addText([
    { text: "지수를 구성할 최적의 지표 조합을 찾으려고", options: { breakLine: true } },
    { text: "유전 알고리즘을 돌렸던 개인 연구입니다.", options: { breakLine: true } },
    { text: "", options: { fontSize: 6, breakLine: true } },
    { text: "여러 후보 지표에 가중치를 주고, 성적이 좋은", options: { breakLine: true } },
    { text: "조합끼리 교배시키면서 세대를 거듭했습니다.", options: {} },
  ], { x: MX + 0.3, y: 3.1, w: lw - 0.6, h: 1.7, fontSize: 12, fontFace: F.sans, color: C.body, align: "left", lineSpacingMultiple: 1.3 });
  s.addText("결과는 실패였습니다. 그런데 방법은 남았습니다.", {
    x: MX + 0.3, y: 5.15, w: lw - 0.6, h: 0.5, fontSize: 12, fontFace: F.serif, bold: true, color: C.accent, align: "left",
  });

  // 중앙 화살표
  s.addText("→", { x: MX + lw + 0.12, y: 3.85, w: 0.6, h: 0.5, fontSize: 24, color: C.mute, align: "center" });

  // 우: 대응 관계
  const rx = MX + lw + 0.8, rw = W - MX - rx;
  s.addText("두 문제는 골격이 같았습니다", { x: rx, y: 2.35, w: rw, h: 0.35, fontSize: 13.5, fontFace: F.serif, bold: true, color: C.ink, align: "left" });
  const rows = [
    ["공포·탐욕 지수", "알파 리서치"],
    ["후보 지표 수십 개", "데이터필드 수천 개"],
    ["지표를 골라 가중치로 결합", "필드를 골라 연산자로 결합"],
    ["백테스트 성적으로 선별", "시뮬레이션 성적으로 선별"],
    ["좋은 조합끼리 교배·변이", "좋은 알파끼리 교배·변이"],
  ];
  let y = 2.85;
  rows.forEach((r, i) => {
    const bold = i === 0;
    s.addText(r[0], { x: rx, y: y, w: rw * 0.47, h: 0.4, fontSize: bold ? 11 : 11.5, fontFace: bold ? F.mono : F.sans, bold, color: bold ? C.mute : C.body, align: "left" });
    s.addText(r[1], { x: rx + rw * 0.51, y: y, w: rw * 0.49, h: 0.4, fontSize: bold ? 11 : 11.5, fontFace: bold ? F.mono : F.sans, bold, color: bold ? C.mute : C.body, align: "left" });
    if (i === 0) s.addShape(pptx.ShapeType.line, { x: rx, y: y + 0.38, w: rw, h: 0, line: { color: C.rule, width: 1 } });
    y += i === 0 ? 0.52 : 0.58;
  });
  s.addText("“조합을 진화시키는 문제”라면 유전 알고리즘이 통한다－그래서 머신에 이식했습니다.", {
    x: MX, y: 6.3, w: CW, h: 0.4, fontSize: 13, fontFace: F.serif, bold: true, color: C.ink, align: "left",
  });
}

// ───────────────────────────── 4. 머신 개괄 + DGX Spark
{
  const s = base("03", "머신 개괄", 4);
  title(s, "머신은 다섯 단계를 돌면서 알파를 만듭니다",
    "한 바퀴가 한 “라운드”입니다. 지금까지 1,138라운드를 돌았고, 사람이 하는 일은 주간 테마 확인과 생체인증뿐입니다.");

  const stages = [
    ["①", "후보 만들기", "유전자를 섞어 후보 알파 20개를 조립"],
    ["②", "사전 검문", "확실히 떨어질 후보를 미리 버림"],
    ["③", "시뮬레이션", "BRAIN에서 성적표를 받아 옴"],
    ["④", "채점·제출 판단", "통과 가능성을 보고 제출을 결정"],
    ["⑤", "복기·학습", "실패 이유를 기록해 다음 라운드에 반영"],
  ];
  const bw = 2.24, gap = 0.15;
  let x = MX;
  stages.forEach((st, i) => {
    panel(s, x, 2.45, bw, 1.75);
    s.addText(st[0], { x: x + 0.15, y: 2.58, w: bw - 0.3, h: 0.35, fontSize: 15, fontFace: F.mono, bold: true, color: C.accent, align: "left" });
    s.addText(st[1], { x: x + 0.15, y: 2.95, w: bw - 0.3, h: 0.35, fontSize: 13.5, fontFace: F.serif, bold: true, color: C.ink, align: "left" });
    s.addText(st[2], { x: x + 0.15, y: 3.32, w: bw - 0.28, h: 0.8, fontSize: 10, fontFace: F.sans, color: C.body, align: "left", lineSpacingMultiple: 1.2 });
    if (i < 4) s.addText("›", { x: x + bw - 0.03, y: 3.1, w: 0.24, h: 0.4, fontSize: 15, color: C.mute, align: "center" });
    x += bw + gap;
  });
  s.addText("↺  ⑤에서 배운 것(엘리트·실패 통계)이 다시 ①의 재료가 됩니다. 이 루프가 24시간 돌아갑니다.", {
    x: MX, y: 4.35, w: CW, h: 0.35, fontSize: 11.5, fontFace: F.sans, color: C.body, align: "left",
  });

  // 하단 밴드: DGX Spark
  panel(s, MX, 4.95, CW, 1.55, C.ink);
  s.addText([
    { text: "API 비용이 감당이 안 돼서, 컴퓨터를 샀습니다", options: { fontSize: 14.5, fontFace: F.serif, bold: true, color: C.paperOnDark, breakLine: true } },
    { text: "", options: { fontSize: 5, breakLine: true } },
    { text: "후보 생성을 전부 클라우드 LLM에 맡기니 호출량이 폭증해 비용을 감당할 수 없었습니다. 그래서 서버용 컴퓨터", options: { fontSize: 11.5, color: C.muteOnDark, breakLine: true } },
    { text: "DGX Spark를 들여 로컬로 전환했습니다. 지금은 Qwen 3.6 35B급 모델이 Fast Expression 생성을 전담합니다.", options: { fontSize: 11.5, color: C.muteOnDark } },
  ], { x: MX + 0.35, y: 5.14, w: CW - 0.7, h: 1.25, fontFace: F.sans, align: "left", lineSpacingMultiple: 1.3 });
}

// ───────────────────────────── 5. 실제 화면 — 관제 (v2.2 신규)
{
  const s = base("04", "실제 화면 — 관제", 5);
  title(s, "지금 이 순간에도 돌고 있습니다",
    "발표 당일(8월 19일) 캡처한 실제 관제 화면입니다. 라운드 390이 돌던 중입니다.");

  shot(s, "ops_stats.png", MX, 2.25, CW, CW / 4.11);

  const calls = [
    ["390", "현재 라운드 · 실행 중", "발표 당일에도 도는 중입니다"],
    ["52", "제출 성공 — 대회 시작 초기화 이후", "표지의 93건은 전체 누적입니다"],
    ["16,544", "게이트 미통과", "걸러낸 만큼이 곧 탐색량입니다"],
  ];
  const cw3 = 3.75, cg3 = 0.3;
  let cx = MX;
  calls.forEach((c2) => {
    s.addText(c2[0], { x: cx, y: 5.35, w: cw3, h: 0.6, fontSize: 26, fontFace: F.serif, bold: true, color: C.ink, align: "left" });
    s.addText(c2[1], { x: cx, y: 5.98, w: cw3, h: 0.3, fontSize: 10.5, fontFace: F.sans, bold: true, color: C.accent, align: "left" });
    s.addText(c2[2], { x: cx, y: 6.28, w: cw3, h: 0.3, fontSize: 10, fontFace: F.sans, color: C.body, align: "left" });
    cx += cw3 + cg3;
  });
}

// ───────────────────────────── 6. 1단계 후보 만들기
{
  const s = base("05", "1단계 — 후보 만들기", 6);
  title(s, "알파 한 줄을 유전자 부품으로 쪼개서 조립합니다",
    "문장을 통째로 생성하지 않습니다. 정해진 부품을 갈아 끼우는 방식이라 교배와 변이가 깔끔해집니다.");

  // 좌: 유전자 구조
  const lw = 5.9;
  s.addText("알파 하나 = 유전자 묶음", { x: MX, y: 2.3, w: lw, h: 0.35, fontSize: 13.5, fontFace: F.serif, bold: true, color: C.ink, align: "left" });
  const genes = [
    ["데이터필드 3개", "신호의 원재료 (수천 종 중 선택)"],
    ["변환", "rank · ts_zscore 처럼 신호의 형태를 다듬음"],
    ["결합", "필드끼리 더할지, 뺄지, 비율로 볼지"],
    ["시간창", "며칠치를 볼지 (2일 ~ 252일)"],
    ["중립화", "시장·업종 공통 움직임을 제거하는 방식"],
    ["감쇠", "신호를 며칠에 걸쳐 부드럽게 쓸지"],
  ];
  let y = 2.75;
  genes.forEach((g) => {
    s.addText([
      { text: g[0], options: { bold: true, color: C.ink, fontFace: F.sans, fontSize: 11.5 } },
      { text: "   " + g[1], options: { color: C.body, fontFace: F.sans, fontSize: 11 } },
    ], { x: MX, y: y, w: lw, h: 0.38, align: "left" });
    y += 0.5;
  });
  s.addText("이 부품 값들을 바꿔 끼우면 완성된 Fast Expression이 렌더링됩니다.", {
    x: MX, y: y + 0.05, w: lw, h: 0.55, fontSize: 11, fontFace: F.sans, italic: true, color: C.mute, align: "left", lineSpacingMultiple: 1.25 });

  // 우: 라운드 구성
  const rx = 7.3, rw = W - MX - rx;
  panel(s, rx, 2.3, rw, 3.9);
  s.addText("한 라운드의 후보 20개 구성 (r198 실측)", { x: rx + 0.28, y: 2.52, w: rw - 0.56, h: 0.35, fontSize: 12.5, fontFace: F.serif, bold: true, color: C.ink, align: "left" });
  const comp = [
    ["변이 10", "엘리트의 유전자 한두 개를 바꾼 자식"],
    ["교차 2", "엘리트 둘의 유전자를 섞은 자식"],
    ["구제 3", "아깝게 떨어진 알파의 부호 반전 등 재도전"],
    ["개선 3", "잘 나온 알파를 더 밀어붙이는 강화판"],
    ["스윕 2", "축 하나만 값을 바꿔 보는 통제 실험"],
  ];
  let ry = 3.0;
  comp.forEach((cRow) => {
    s.addText(cRow[0], { x: rx + 0.28, y: ry, w: 1.35, h: 0.38, fontSize: 12, fontFace: F.mono, bold: true, color: C.accent, align: "left" });
    s.addText(cRow[1], { x: rx + 1.7, y: ry, w: rw - 1.95, h: 0.38, fontSize: 11, fontFace: F.sans, color: C.body, align: "left" });
    ry += 0.52;
  });
  s.addShape(pptx.ShapeType.line, { x: rx + 0.28, y: ry + 0.05, w: rw - 0.56, h: 0, line: { color: C.rule, width: 1 } });
  s.addText("여기에 무작위 탐색 슬롯과 LLM 위원회의 전략 스펙이 별도로 더해집니다.", {
    x: rx + 0.28, y: ry + 0.15, w: rw - 0.56, h: 0.55, fontSize: 10.5, fontFace: F.sans, color: C.body, align: "left", lineSpacingMultiple: 1.25 });
  s.addText("역할 분담 : LLM 위원회는 가설과 방향을 내고, 유전 알고리즘은 그 안에서 끈질기게 반복 탐색합니다.", {
    x: MX, y: 6.45, w: CW, h: 0.35, fontSize: 12, fontFace: F.serif, bold: true, color: C.ink, align: "left",
  });
}

// ───────────────────────────── 7. 2단계 사전 검문
{
  const s = base("06", "2단계 — 사전 검문", 7);
  title(s, "확실히 떨어질 후보는 시뮬레이션 전에 버립니다",
    "시뮬 슬롯은 8개뿐입니다. 실패가 뻔한 후보를 태우면 그 시간만큼 다음 실험이 밀립니다.");

  const gates = [
    ["01", "문법·단위 검사", "수식이 문법에 맞는지, 단위가 호환되는지 렌더링 직후 확인합니다. 틀리면 접수조차 안 합니다."],
    ["02", "필드 가용성", "지금 탐색 중인 리전·유니버스에 존재하지 않는 필드, 금지된 데이터셋 필드를 걸러냅니다."],
    ["03", "중복 차단", "같은 라운드의 형제와 겹치거나, 과거에 이미 제출·거절된 수식과 같으면 제외합니다."],
    ["04", "테마 조건", "그 주의 Power Pool 테마(리전·유니버스·필수 체크)를 생성 조건에 직접 넣어 위반을 막습니다."],
    ["05", "차단 게이트", "성적이 나와도 하드 탈락 사유가 하나라도 있으면 제출을 시도하지 않습니다. 하루 4건 예산의 마지막 잠금장치입니다."],
  ];
  let y = 2.35;
  gates.forEach((g) => {
    s.addText(g[0], { x: MX, y: y, w: 0.6, h: 0.4, fontSize: 15, fontFace: F.mono, bold: true, color: C.accent, align: "left" });
    s.addText([
      { text: g[1] + "   ", options: { bold: true, fontFace: F.serif, fontSize: 13, color: C.ink } },
      { text: g[2], options: { fontFace: F.sans, fontSize: 11.5, color: C.body } },
    ], { x: MX + 0.65, y: y, w: CW - 0.65, h: 0.6, align: "left", lineSpacingMultiple: 1.2 });
    y += 0.78;
  });
  s.addText("여기서 떨어진 후보는 시뮬레이션 38,431건에 아예 포함되지 않습니다. 거른 만큼이 곧 탐색량입니다.", {
    x: MX, y: 6.4, w: CW, h: 0.35, fontSize: 12, fontFace: F.serif, bold: true, color: C.accent, align: "left",
  });
}

// ───────────────────────────── 8. 3단계 시뮬레이션
{
  const s = base("07", "3단계 — 시뮬레이션", 8);
  title(s, "여덟 개 슬롯으로 하루 평균 364건을 돌립니다",
    "IQC 때처럼 브라우저를 조작하지 않습니다. BRAIN REST API를 직접 불러 접수하고 회수합니다.");

  const stats = [
    ["8", "동시 슬롯", "계정이 동시에 돌릴 수 있는 시뮬레이션 수"],
    ["2~20분", "1건 소요", "대기열 포함. 그래서 슬롯 낭비가 가장 비쌉니다"],
    ["364건", "일평균 시뮬", "대기열·재시도 포함 103일 운영 실측"],
  ];
  const bw2 = 3.75, gap2 = 0.3;
  let x = MX;
  stats.forEach((st) => {
    panel(s, x, 2.4, bw2, 2.1);
    s.addText(st[0], { x: x + 0.3, y: 2.6, w: bw2 - 0.6, h: 0.8, fontSize: 34, fontFace: F.serif, bold: true, color: C.ink, align: "left" });
    s.addText(st[1], { x: x + 0.3, y: 3.45, w: bw2 - 0.6, h: 0.3, fontSize: 11.5, fontFace: F.sans, bold: true, color: C.accent, align: "left" });
    s.addText(st[2], { x: x + 0.3, y: 3.78, w: bw2 - 0.6, h: 0.6, fontSize: 10.5, fontFace: F.sans, color: C.body, align: "left", lineSpacingMultiple: 1.2 });
    x += bw2 + gap2;
  });

  const notes = [
    "플랫폼의 속도 제한(Retry-After)을 지키면서 슬롯이 빌 때마다 다음 후보를 밀어 넣습니다.",
    "오래 멈춰 있는 시뮬레이션은 잘라내서 슬롯을 회수합니다. 실행 중인 것을 죽이지 않도록 진행률로 판별합니다.",
    "성공이든 실패든 결과 전부를 원장(DB)에 기록합니다. 이 기록이 4·5단계 판단의 근거가 됩니다.",
  ];
  let y = 4.95;
  notes.forEach((n) => {
    s.addText([
      { text: "—  ", options: { color: C.accent, bold: true } },
      { text: n, options: { color: C.body } },
    ], { x: MX, y: y, w: CW, h: 0.4, fontSize: 12, fontFace: F.sans, align: "left" });
    y += 0.5;
  });
}

// ───────────────────────────── 9. 4단계 채점·제출 판단
{
  const s = base("08", "4단계 — 채점과 제출 판단", 9);
  title(s, "점수만 보지 않고 “통과할 수 있는가”를 판단합니다",
    "Sharpe가 높다고 제출되는 게 아닙니다. 플랫폼의 체크를 전부 통과해야 등재됩니다.");

  // 좌: 채점
  const lw = 5.9;
  s.addText("채점：체크 배터리", { x: MX, y: 2.3, w: lw, h: 0.35, fontSize: 13.5, fontFace: F.serif, bold: true, color: C.ink, align: "left" });
  s.addText([
    { text: "시뮬 결과를 Sharpe · Fitness · 회전율 · 기존 알파와의", options: { breakLine: true } },
    { text: "상관 등 여러 체크로 채점합니다.", options: { breakLine: true } },
    { text: "", options: { fontSize: 6, breakLine: true } },
    { text: "어느 체크가 진짜 탈락 사유(하드)이고 어느 것은 경고", options: { breakLine: true } },
    { text: "(소프트)인지는 하드코딩하지 않습니다. 원장에 쌓인", options: { breakLine: true } },
    { text: "거절 217건 · 성공 28건(최근 14일)을 비교해서", options: { breakLine: true } },
    { text: "매일 다시 학습합니다. 플랫폼이 기준을 바꿔도", options: { breakLine: true } },
    { text: "규칙이 따라갑니다.", options: {} },
  ], { x: MX, y: 2.75, w: lw, h: 3.0, fontSize: 12, fontFace: F.sans, color: C.body, align: "left", lineSpacingMultiple: 1.35 });

  // 우: 제출 규칙 3
  const rx = 7.3, rw = W - MX - rx;
  s.addText("제출：운영에서 배운 세 가지", { x: rx, y: 2.3, w: rw, h: 0.35, fontSize: 13.5, fontFace: F.serif, bold: true, color: C.ink, align: "left" });
  const facts = [
    ["하루 4건 상한", "아무리 많이 찾아도 등재는 하루 4건까지입니다."],
    ["거절은 쿼터를 안 깎음", "실패한 시도는 한도를 쓰지 않습니다. 그래서 하드 차단만 없으면 일단 시도해서 실제 판정을 받습니다."],
    ["못 쏜 알파는 대기 큐로", "쿼터가 찬 뒤의 통과작은 버리지 않고 다음 리셋 직후 자동 발사합니다."],
  ];
  let y = 2.78;
  facts.forEach((f2, i) => {
    s.addText([
      { text: String(i + 1) + "  ", options: { fontFace: F.mono, bold: true, color: C.accent, fontSize: 13 } },
      { text: f2[0], options: { bold: true, fontFace: F.serif, fontSize: 12.5, color: C.ink } },
    ], { x: rx, y: y, w: rw, h: 0.35, align: "left" });
    s.addText(f2[1], { x: rx + 0.35, y: y + 0.36, w: rw - 0.35, h: 0.62, fontSize: 11, fontFace: F.sans, color: C.body, align: "left", lineSpacingMultiple: 1.22 });
    y += i === 0 ? 0.95 : 1.18;
  });
  s.addText("결론 : 머신의 목표는 “세게”가 아니라 “통과하게” 만드는 것입니다.", {
    x: MX, y: 6.4, w: CW, h: 0.35, fontSize: 12.5, fontFace: F.serif, bold: true, color: C.accent, align: "left",
  });
}

// ───────────────────────────── 10. 5단계 복기·학습
{
  const s = base("09", "5단계 — 복기와 학습", 10);
  title(s, "실패한 이유가 다음 변이의 방향을 정합니다",
    "무작위로 흔들지 않습니다. 어느 관문에서 떨어졌는지를 보고 바꿀 부품을 지목합니다.");

  // 좌: 정향변이 사상
  const lw = 5.9;
  s.addText("실패 원인 → 변이 지시", { x: MX, y: 2.3, w: lw, h: 0.35, fontSize: 13.5, fontFace: F.serif, bold: true, color: C.ink, align: "left" });
  const maps = [
    ["회전율이 너무 높음", "감쇠를 늘리고 시간창을 넓혀 부드럽게"],
    ["회전율이 너무 낮음", "감쇠를 줄이고 짧은 창으로 날카롭게"],
    ["비중이 한쪽에 쏠림", "중립화 방식과 이상치 절단을 조정"],
    ["작은 종목군에서 무너짐", "더 큰 유니버스로 확장"],
    ["시간이 지나며 불안정", "장기 안정성을 높이는 쪽으로 보강"],
  ];
  let y = 2.78;
  maps.forEach((m) => {
    s.addText([
      { text: m[0], options: { bold: true, color: C.ink, fontSize: 11.5 } },
      { text: "  →  " + m[1], options: { color: C.body, fontSize: 11 } },
    ], { x: MX, y: y, w: lw, h: 0.4, fontFace: F.sans, align: "left" });
    y += 0.56;
  });
  s.addText("변이 방식별 성공률도 기록해서, 잘 통하는 방식에 다음 슬롯을 더 줍니다(밴딧).", {
    x: MX, y: y + 0.08, w: lw, h: 0.6, fontSize: 11, fontFace: F.sans, italic: true, color: C.mute, align: "left", lineSpacingMultiple: 1.25 });

  // 우: 한 축 스윕
  const rx = 7.3, rw = W - MX - rx;
  panel(s, rx, 2.3, rw, 3.95);
  s.addText("원칙 : 한 번에 한 축만 바꾼다", { x: rx + 0.28, y: 2.52, w: rw - 0.56, h: 0.35, fontSize: 13, fontFace: F.serif, bold: true, color: C.ink, align: "left" });
  s.addText([
    { text: "여러 부품을 동시에 바꾸면 성과가 좋아져도 무엇", options: { breakLine: true } },
    { text: "덕분인지 알 수 없습니다. 부모·자식 10,119쌍 실측 —", options: {} },
  ], { x: rx + 0.28, y: 2.95, w: rw - 0.56, h: 0.7, fontSize: 11, fontFace: F.sans, color: C.body, align: "left", lineSpacingMultiple: 1.25 });
  s.addText([
    { text: "1개 변경  개선율 20.3%", options: { bold: true, color: C.ink, fontFace: F.mono, fontSize: 13, breakLine: true } },
    { text: "6개 이상  개선율 6.2%", options: { color: C.mute, fontFace: F.mono, fontSize: 13 } },
  ], { x: rx + 0.28, y: 3.72, w: rw - 0.56, h: 0.75, align: "left", lineSpacingMultiple: 1.4 });
  s.addShape(pptx.ShapeType.line, { x: rx + 0.28, y: 4.62, w: rw - 0.56, h: 0, line: { color: C.rule, width: 1 } });
  s.addText([
    { text: "실제 사례", options: { bold: true, fontFace: F.serif, fontSize: 11.5, color: C.ink, breakLine: true } },
    { text: "중립화만 바꿈 → Sharpe 1.72 → 2.47", options: { fontFace: F.mono, fontSize: 10.5, color: C.body, breakLine: true } },
    { text: "감쇠만 바꿈 → 회전율 100.5% → 48.5%", options: { fontFace: F.mono, fontSize: 10.5, color: C.body } },
  ], { x: rx + 0.28, y: 4.75, w: rw - 0.56, h: 1.3, align: "left", lineSpacingMultiple: 1.4 });
  s.addText("복기가 쌓일수록 머신은 “어디를 만지면 무엇이 변하는지”를 배웁니다.", {
    x: MX, y: 6.45, w: CW, h: 0.35, fontSize: 12, fontFace: F.serif, bold: true, color: C.ink, align: "left",
  });
}

// ───────────────────────────── 11. 엘리트 선정
{
  const s = base("10", "엘리트 선정", 11);
  title(s, "다섯 과목으로 뽑힌 엘리트가 다음 라운드의 부모입니다",
    "Sharpe 하나로만 뽑으면 우승자 가족이 풀을 독점합니다. 그래서 다섯 잣대로, 서로 다른 유형이 남게 뽑습니다.");

  const axes = [
    ["신호의 세기", "Sharpe"],
    ["효율", "Fitness (회전율 대비)"],
    ["통과 근접도", "제출 체크에 얼마나 가까운가"],
    ["남들과의 거리", "기존 제출작과의 상관 (낮을수록 좋음)"],
    ["시간 안정성", "최근 2년 구간의 Sharpe"],
  ];
  const bw3 = 2.24, gap3 = 0.15;
  let x = MX;
  axes.forEach((a, i) => {
    panel(s, x, 2.35, bw3, 1.5);
    s.addText(String(i + 1), { x: x + 0.15, y: 2.47, w: bw3 - 0.3, h: 0.3, fontSize: 12, fontFace: F.mono, bold: true, color: C.accent, align: "left" });
    s.addText(a[0], { x: x + 0.15, y: 2.76, w: bw3 - 0.3, h: 0.32, fontSize: 12, fontFace: F.serif, bold: true, color: C.ink, align: "left" });
    s.addText(a[1], { x: x + 0.15, y: 3.1, w: bw3 - 0.28, h: 0.65, fontSize: 9.5, fontFace: F.sans, color: C.body, align: "left", lineSpacingMultiple: 1.2 });
    x += bw3 + gap3;
  });

  const rules = [
    ["다섯 과목을 한 점수로 합치지 않습니다", "NSGA-II 방식입니다. 어떤 과목에서든 최선인 후보들을 한 묶음(파레토 프런트)으로 남깁니다."],
    ["비슷한 애들끼리는 한 명만 남깁니다", "같은 묶음 안에서는 서로 멀리 떨어진(혼잡 거리가 큰) 개체를 우선합니다. 다양성이 곧 제출 가능성입니다."],
    ["오래된 우승자는 밀어냅니다", "최근 성적 위주로 엘리트를 다시 뽑아, 한 번 잘 나온 가계가 자리를 독점하지 못하게 합니다."],
  ];
  let y = 4.25;
  rules.forEach((r) => {
    s.addText([
      { text: r[0] + "   ", options: { bold: true, fontFace: F.serif, fontSize: 12.5, color: C.ink } },
      { text: r[1], options: { fontFace: F.sans, fontSize: 11.5, color: C.body } },
    ], { x: MX, y: y, w: CW, h: 0.62, align: "left", lineSpacingMultiple: 1.22 });
    y += 0.72;
  });
  s.addText("이렇게 뽑힌 엘리트가 1단계의 변이·교배 재료가 됩니다. 현재 가장 깊은 혈통은 136세대(g136)입니다.", {
    x: MX, y: 6.45, w: CW, h: 0.35, fontSize: 12, fontFace: F.serif, bold: true, color: C.accent, align: "left",
  });
}

// ───────────────────────────── 12. 실제 화면 — 진화 분석 (v2.2 신규)
{
  const s = base("11", "실제 화면 — 진화 분석", 12);
  title(s, "다섯 단계가 쌓이면 이런 그림이 됩니다",
    "같은 날 캡처한 진화 분석 화면입니다. 왼쪽이 혈통 지도, 오른쪽이 복기의 결과물입니다.");

  const tw = 7.55;
  shot(s, "trace.png", MX, 2.3, tw, tw / 2.48,
    "진화 궤적 — 점 하나가 알파 한 건, 선이 부모→자식 계보입니다 (최근 50라운드)");

  const dx = MX + tw + 0.35, dw = W - MX - dx;
  shot(s, "directive.png", dx, 2.3, dw, dw / 1.75,
    "정향변이 학습 — 실패 사유별로 어느 부품을 고치면 통했는지 성공률을 집계합니다");

  s.addText("엘리트 하나가 자식들을 부챗살처럼 퍼뜨리는 모양이 궤적에 그대로 남습니다 — 진화가 실제로 일어난다는 증거입니다.", {
    x: MX, y: 6.45, w: CW, h: 0.35, fontSize: 12, fontFace: F.serif, bold: true, color: C.ink, align: "left",
  });
}

// ───────────────────────────── 13. 실제 라운드 r198
{
  const s = base("12", "실전 기록", 13);
  title(s, "실제로 돌아간 한 라운드 (r198, 8월 14일 오후)",
    "원장에 남은 로그를 그대로 옮겼습니다. 위 다섯 단계가 18분 동안 이렇게 흘러갑니다.");

  const rows = [
    ["15:47:55", "라운드 개시", "부모 = 직전 최고 엘리트 #6. 실패 사유 “LOW_FITNESS 0.6 < 1.0”을 읽고 개선 라운드로 진입."],
    ["15:47:55", "탐색 조건 확정", "GLB · D1 · TOPDIV3000 · 금지 데이터셋 5종 제외 · 요구 체크 HT_HIGH_TURNOVER_RETURNS_RATIO."],
    ["15:47:56", "세대 구성", "변이 10 · 교차 2 · 구제 변형 3(부호 반전·사후 감쇠·RAM 중립화) · 개선 레이어 3 · 스윕 2 = 후보 20."],
    ["15:47:56", "사전 검문", "기존 조합 4개는 근처의 새 변형으로 교체, 이미 제출·거절된 같은 식 2개는 제외. 모두 시뮬 전에 처리."],
    ["15:58~16:06", "시뮬 결과 회수", "#1 스윕(중립화 축) S 0.43 · #2 스윕(변환 축) S 2.02 fit 0.97 · #5 변이 S 1.42 · #6 변이 S 1.44."],
    ["16:06:05", "제출", "#6이 전 체크 통과(7 PASS / 0 FAIL) → 자동 제출 성공. #2는 S가 더 높았지만 PROD_CORRELATION으로 차단."],
  ];
  let y = 2.3;
  rows.forEach((r, i) => {
    s.addText(r[0], { x: MX, y: y, w: 1.55, h: 0.4, fontSize: 10.5, fontFace: F.mono, bold: true, color: C.accent, align: "left" });
    s.addText([
      { text: r[1] + "   ", options: { bold: true, fontFace: F.serif, fontSize: 12, color: C.ink } },
      { text: r[2], options: { fontFace: F.sans, fontSize: 11, color: C.body } },
    ], { x: MX + 1.7, y: y, w: CW - 1.7, h: 0.6, align: "left", lineSpacingMultiple: 1.2 });
    if (i < rows.length - 1) s.addShape(pptx.ShapeType.line, { x: MX + 1.7, y: y + 0.6, w: CW - 1.7, h: 0, line: { color: C.rule, width: 0.75 } });
    y += 0.66;
  });
  s.addText("가장 센 알파(#2, S 2.02)가 아니라 통과한 알파(#6, S 1.44)가 등재됐습니다. 신호가 세다고 제출되는 게 아닙니다.", {
    x: MX, y: 6.4, w: CW, h: 0.4, fontSize: 12.5, fontFace: F.serif, bold: true, color: C.accent, align: "left",
  });
}

// ───────────────────────────── 14. 심사 기준 매핑 (v2.2 신규)
{
  const s = base("13", "심사 기준", 14);
  title(s, "다섯 가지 심사 기준으로 보면",
    "공지된 심사 기준에 이 머신을 하나씩 얹어 봤습니다.");

  const crit = [
    ["생산성", "103일 동안 시뮬 38,431건 · 1,138라운드 · 제출 93건. 하루 4건 상한은 대기 큐로 끝까지 소화합니다."],
    ["피라미드 확장성", "리전 × 딜레이 × 카테고리 칸을 목표로 삼는 다변화 웨이브가 피라미드의 미달 칸부터 채웁니다."],
    ["오버피팅 방지", "최근 2년 구간 안정성 · 상관 게이트 · 한 축 변경 원칙 — 세 겹의 방어선이 과거 성적만 좇는 것을 막습니다."],
    ["알파 템플릿 확장성", "유전자 부품 조합 1만 3천여 가지에 강신호 골격 8종. 새 데이터필드도 부품만 갈아 끼우면 바로 흡수됩니다."],
    ["임팩트", "사람 손 거의 없이 24시간 도는 알파 공장입니다. 로컬 LLM 전환으로 추가 API 비용 없이 돌아갑니다."],
  ];
  const lw2 = 7.0;
  let y = 2.35;
  crit.forEach((c2, i) => {
    s.addText(String(i + 1), { x: MX, y: y, w: 0.45, h: 0.4, fontSize: 15, fontFace: F.mono, bold: true, color: C.accent, align: "left" });
    s.addText([
      { text: c2[0] + "   ", options: { bold: true, fontFace: F.serif, fontSize: 13, color: C.ink } },
      { text: c2[1], options: { fontFace: F.sans, fontSize: 11, color: C.body } },
    ], { x: MX + 0.5, y: y, w: lw2 - 0.5, h: 0.75, align: "left", lineSpacingMultiple: 1.22 });
    y += 0.85;
  });

  const rx = MX + lw2 + 0.45, rw = W - MX - rx;
  shot(s, "submit_history.png", rx, 2.35, rw, rw / 2.93,
    "제출 내역 — 자동 제출이 실제로 쌓입니다");
  shot(s, "leaderboard.png", rx, 4.45, rw, rw / 2.81,
    "원장 리더보드 — 상위 알파의 통과 체크 9/9");
}

// ───────────────────────────── 15. 앞으로
{
  const s = base("14", "다음", 15);
  title(s, "더 많이가 아니라, 더 다르게 찾는 머신으로",
    "제출량을 좇은 대가로 제출작끼리 닮아가는 문제(상관 포화)가 왔습니다. 다음 버전은 여기를 고칩니다.");

  const plans = [
    ["01", "쏠림 줄이기", "제출작이 두 데이터 패밀리에 88% 몰려 있습니다. 피라미드에서 아직 못 채운 칸을 우선하고, Analyst 4,218필드 · Fundamental 4,428필드에 새 혈통을 심습니다."],
    ["02", "유전자 다이어트", "실측에서 결과를 거의 안 바꾸는 부품(절단·결측 처리)은 탐색 축에서 내립니다. 아낀 슬롯은 신호를 실제로 바꾸는 데이터필드와 변환에 몰아줍니다."],
    ["03", "성적표 교체", "머신의 점수를 “제출 건수”에서 “상관 관문을 통과한 서로 다른 혈통 수”로 바꿉니다. 자기 자신과 경쟁하는 낭비를 지표 단계에서 막습니다."],
  ];
  let y = 2.35;
  plans.forEach((p) => {
    s.addText(p[0], { x: MX, y: y, w: 0.65, h: 0.45, fontSize: 16, fontFace: F.mono, bold: true, color: C.accent, align: "left" });
    s.addText([
      { text: p[1] + "   ", options: { bold: true, fontFace: F.serif, fontSize: 13.5, color: C.ink } },
      { text: p[2], options: { fontFace: F.sans, fontSize: 11.5, color: C.body } },
    ], { x: MX + 0.7, y: y, w: CW - 0.7, h: 0.85, align: "left", lineSpacingMultiple: 1.25 });
    y += 1.05;
  });

  s.addShape(pptx.ShapeType.line, { x: MX, y: 5.85, w: CW, h: 0, line: { color: C.rule, width: 1 } });
  s.addText("다음 버전은 더 많이 찾는 머신이 아니라, 더 다르게 찾는 머신입니다.", {
    x: MX, y: 6.05, w: CW, h: 0.4, fontSize: 13.5, fontFace: F.serif, bold: true, color: C.accent, align: "left",
  });
}

// ───────────────────────────── 16. Q&A — 감사합니다
{
  const s = pptx.addSlide();
  s.background = { color: C.ink };
  s.addText("2026 컨설턴트 서머 부트캠프  ·  머신 발표회", {
    x: MX, y: 1.05, w: CW, h: 0.35, fontSize: 12, fontFace: F.sans, color: C.muteOnDark, align: "left",
  });
  s.addText("Q & A", {
    x: MX, y: 2.05, w: CW, h: 1.3, fontSize: 60, fontFace: F.serif, bold: true, color: C.paperOnDark, align: "left",
  });
  s.addShape(pptx.ShapeType.line, { x: MX + 0.03, y: 3.48, w: 2.2, h: 0, line: { color: C.accent, width: 2.5 } });
  s.addText("감사합니다", {
    x: MX, y: 3.75, w: CW, h: 0.7, fontSize: 26, fontFace: F.serif, bold: true, color: C.paperOnDark, align: "left",
  });
  s.addText("기계는 오늘 밤에도 돌아갑니다.", {
    x: MX, y: 4.55, w: CW, h: 0.4, fontSize: 13, fontFace: F.sans, color: C.muteOnDark, align: "left",
  });
  s.addText([
    { text: "김현호", options: { bold: true, color: C.paperOnDark } },
    { text: "      GenomicWQB  ·  2026. 08. 19", options: { color: C.muteOnDark } },
  ], { x: MX, y: 6.55, w: CW, h: 0.35, fontSize: 12, fontFace: F.sans, align: "left" });
}

pptx.writeFile({ fileName: "GenomicWQB_머신발표_v2.pptx" }).then(() => console.log("done"));
