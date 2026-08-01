/**
 * 模板解析：浏览器牵线，vektor3d 出力，SDM 存正本。
 *
 * 三方各自的位置决定了这条链路只能这么走：
 *   SDM 后端在 Linux 集群，**永远调不通**装在用户 Windows 桌面、只监听 localhost
 *   的 vektor3d；反过来桌面能访问集群。所以浏览器发起调用，只递 URL + 票据，
 *   正文由 vektor3d 自己 GET 拉走，解析结果再由浏览器带用户令牌写回 SDM。
 *   与 geometry.convert / mesh.* 完全同一套机制，见 api/vektor3d.ts 顶部注释。
 *
 * 为什么解析结果存在 SDM 而不留在 vektor3d：库的正本在 SDM（材料库、控制卡库都
 * 在这里维护），vektor3d 是使用方。它本地那份是缓存，靠 sdmTemplateId + version
 * 命中，版本一变就重拉——所以这里必须把 version 传准。
 */
import { simApi } from "@/api";
import type { SimTemplateParseResult, SimTemplateParseTicket } from "@/api/types";
import { vektor3d, Vektor3dError } from "@/api/vektor3d";

export type ParsePhase = "" | "签发票据" | "解析中" | "写回";

export interface ParseOutcome {
  result: SimTemplateParseResult;
  /** ERR 级问题：闭包不完整、必需卡缺失这类**求解器不会报错但结果是错的**问题 */
  errors: string[];
  warnings: string[];
}

function split(issues: SimTemplateParseResult["issues"]) {
  const pick = (lv: string) =>
    (issues ?? []).filter((i) => i.level === lv)
      .map((i) => (i.keyword ? `${i.keyword}: ${i.message}` : i.message));
  return { errors: pick("ERR"), warnings: pick("WARN") };
}

/** 解析一份模板并把摘要写回 SDM。onPhase 用于在按钮上显示进度。 */
export async function parseTemplate(
  kind: "material" | "control",
  tid: string,
  onPhase?: (p: ParsePhase, detail?: string) => void
): Promise<ParseOutcome> {
  onPhase?.("签发票据");
  const t: SimTemplateParseTicket =
    kind === "material" ? await simApi.materialParseTicket(tid)
      : await simApi.controlParseTicket(tid);

  onPhase?.("解析中");
  const result = await vektor3d.runJob<SimTemplateParseResult>(
    "cae.template.parse",
    {
      kind,
      sourceUrl: t.sourceUrl,
      authToken: t.token,
      sourceName: t.sourceName,
      // 控制卡自身推不出单位制，必须由 SDM 声明；材料可省略让解析器反推，
      // 但我们仍然传：传了就等于让能力侧顺带复核 SDM 记的单位制对不对。
      unitSystem: t.unitSystem,
      sdmTemplateId: t.tid,
      sdmVersion: t.version,
      ...(t.expectedSha256 ? { expectedSha256: t.expectedSha256 } : {}),
    },
    {
      // 同一份内容重复点"重新解析"不该重跑：idempotencyKey 带上版本，
      // 版本没变就直接拿上次结果，变了才是一次新作业。
      idempotencyKey: `sdm-tpl-${kind}-${tid}-${t.version}`,
      onProgress: (p) => onPhase?.("解析中", p?.step || ""),
    }
  );

  onPhase?.("写回");
  const summary = JSON.stringify({
    ...(result.summary ?? {}),
    unitSystem: result.unitSystem,
    sourceSha256: result.sourceSha256,
    issues: result.issues ?? [],
    parsedAt: Date.now(),
  });
  if (kind === "material") await simApi.updateMaterialTemplate(tid, { summary_json: summary });
  else await simApi.updateControlTemplate(tid, { summary_json: summary });

  onPhase?.("");
  return { result, ...split(result.issues) };
}

/**
 * 解析失败不该挡住入库。
 *
 * 桌面端没开、端口不对、令牌没配——这些都很常见，而模板本身已经存进 SDM 了。
 * 把"没解析成"退化成一条提示，用户回头点"重新解析"即可；反之若因为 vektor3d
 * 不在就拒绝入库，SDM 就成了 vektor3d 的下游，方向正好反了。
 */
export function parseHint(e: unknown): string {
  if (e instanceof Vektor3dError) {
    if (e.code === "unreachable") return "已入库；vektor3d 未连接，稍后可点「解析」补摘要";
    return `已入库；vektor3d 解析失败（${e.message}），可稍后重试`;
  }
  return "已入库；解析未完成，可稍后重试";
}
