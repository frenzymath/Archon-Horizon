import assert from "node:assert/strict";
import { test } from "node:test";
import { renderToPipeableStream, renderToStaticMarkup } from "react-dom/server";
import { PassThrough } from "node:stream";
import type { ReactNode } from "react";
import katex from "katex";
import { bibliographyEntriesFromMarkdown, parseBibliography, safeReferenceUrl } from "../src/utils/bibliography";
import { referencedNodeIds } from "../src/utils/blueprintSyntax";
import BlueprintMarkdown from "../src/components/BlueprintMarkdown";
import Bibliography from "../src/components/Bibliography";
import RoadmapMarkdown from "../src/components/RoadmapMarkdown";
import MathTitle from "../src/components/MathTitle";
import DocumentView from "../src/components/DocumentView";
import FormalizationDAG from "../src/components/FormalizationDAG";
import { NodeProgressLabels, TagList } from "../src/components/TagList";
import { NodeSessionTags } from "../src/components/SessionActivityTags";
import { repositoryUrl, sourceContext, sourceHref } from "../src/utils/sourceLinks";
import { composeDocument, documentBody, splitDocument } from "../src/utils/document";

function renderAsync(element: ReactNode): Promise<string> {
  return new Promise((resolve, reject) => {
    const output = new PassThrough();
    const chunks: string[] = [];
    output.on("data", (chunk) => chunks.push(chunk.toString()));
    output.on("end", () => resolve(chunks.join("")));
    output.on("error", reject);
    const stream = renderToPipeableStream(element, { onAllReady: () => stream.pipe(output), onError: reject });
  });
}

test("graph progress separates source reports, current checks and node labels", () => {
  const nodes = [
    { id: "checked", status: "open", completion: { current_kernel_checked: true, source_reported_complete: true, exact_claim_closed: false } },
    { id: "source", status: "open", completion: { source_reported_complete: true } },
    { id: "historical", status: "open", completion: { historical_kernel_checked: true, current_kernel_checked: false } },
    { id: "stale", status: "stale", completion: { historical_kernel_checked: true, current_kernel_checked: false } },
    { id: "closed", status: "formalized", labels: ["formally_proved"], completion: { current_kernel_checked: true, exact_claim_closed: true } },
    { id: "legacy", status: "open", formally_verified: true, metadata: { source_leanok: true } },
    { id: "archived", lifecycle: "archived", completion: { current_kernel_checked: true, source_reported_complete: true } },
  ];
  const graph = { scope: "project", nodes };
  const html = renderToStaticMarkup(<FormalizationDAG graph={graph} focusId="checked" onOpenNode={() => {}} />);
  assert.match(html, /6 nodes \/ 1 formally proved/);
  assert.match(html, /2 kernel checked/);
  assert.match(html, /2 source complete/);
  assert.match(html, /Kernel check recorded/);
  assert.match(html, /Source reports complete/);
  assert.match(html, /Node is not formally proved/);
  assert.match(html, /<i class="formalized"><\/i> Formally Proved/);
  assert.match(html, /<i class="formally_stated"><\/i> Formally Stated/);
  assert.match(html, /<i class="informal_proved"><\/i> Informal Proved/);
  assert.match(html, /<i class="proof_sketch"><\/i> Proof Sketch/);
  assert.match(html, /<i class="informal_stated"><\/i> Informal Stated/);

  const closed = renderToStaticMarkup(<FormalizationDAG graph={graph} focusId="closed" onOpenNode={() => {}} />);
  assert.doesNotMatch(closed, /Node is not formally proved/);
  const historical = renderToStaticMarkup(<FormalizationDAG graph={graph} focusId="historical" onOpenNode={() => {}} />);
  assert.doesNotMatch(historical, /Kernel check recorded|Source reports complete/);
});

test("node session tags link to activity", () => {
  const activities = [{ attempt_id: "attempt-1", run_id: "run-live", session_id: "session-live", session_number: 1403, harness: "codex", host_id: "worker" }];
  const node = { created_session_id: "session-create", created_run_id: "run-create", updated_session_id: "session-update", updated_run_id: "run-update" };
  const html = renderToStaticMarkup(<NodeSessionTags node={node} activities={activities} />);
  assert.match(html, /#1403 running/);
  assert.match(html, /href="\/\?tab=activity&amp;run=run-live&amp;session=session-live"/);
  assert.match(html, />Created</);
  assert.match(html, /href="\/\?tab=activity&amp;run=run-create&amp;session=session-create"/);
  assert.match(html, />Updated</);
  assert.match(html, /href="\/\?tab=activity&amp;run=run-update&amp;session=session-update"/);
  const same = renderToStaticMarkup(<NodeSessionTags node={{ created_session_id: "session-create", created_run_id: "run-create", updated_session_id: "session-create", updated_run_id: "run-create" }} />);
  assert.match(same, /Created in session/);
  assert.doesNotMatch(same, />Updated</);
  assert.equal(renderToStaticMarkup(<NodeSessionTags node={{ id: "plain" }} />), "");
});

test("DAG detail shows live and creating node session tags", () => {
  const graph = { scope: "project", nodes: [{ id: "goal", title: "Goal", created_session_id: "session-create", created_run_id: "run-create" }] };
  const activities = new Map([["goal", [{ attempt_id: "attempt-1", run_id: "run-live", session_id: "session-live", session_number: 1403, harness: "codex", host_id: "worker" }]]]);
  const html = renderToStaticMarkup(<FormalizationDAG graph={graph} focusId="goal" onOpenNode={() => {}} nodeActivityByNode={activities} />);
  assert.match(html, /#1403 running/);
  assert.match(html, /href="\/\?tab=activity&amp;run=run-live&amp;session=session-live"/);
  assert.match(html, />Created</);
  assert.match(html, /href="\/\?tab=activity&amp;run=run-create&amp;session=session-create"/);
});

test("DAG and node labels appear once in progress colors", () => {
  const node = { id: "m05", title: "M05: Hamilton-Ivey pinching persistence", kind: "claim", labels: ["milestone", "formally_stated"], document: "---\ntitle: M05\nlabels: [milestone, formally_stated]\ntags: [milestone]\n---\n\nStatement.\n" };
  const html = renderToStaticMarkup(<FormalizationDAG graph={{ scope: "project", nodes: [node] }} focusId="m05" onOpenNode={() => {}} />);
  assert.match(html, /aria-label="Graph legend"/);
  assert.match(html, /<i class="formally_stated"><\/i> Formally Stated/);
  assert.equal((html.match(/progress-formally_stated/g) || []).length, 1);
  assert.equal((html.match(/progress-milestone/g) || []).length, 1);
  assert.doesNotMatch(html, /formalization-dag-status formally_stated/);
  const labels = renderToStaticMarkup(<NodeProgressLabels labels={["informal_stated", "formally_stated", "milestone", "formally_stated"]} />);
  assert.match(labels, /progress-milestone">Milestone/);
  assert.match(labels, /progress-formally_stated">Formally Stated/);
  assert.match(labels, /progress-informal_stated">Informal Stated/);
  assert.equal((labels.match(/Formally Stated/g) || []).length, 1);
});

test("live node activity prefers the mission over a raw session id", () => {
  const html = renderToStaticMarkup(<NodeSessionTags projectId="poincare-conjecture" activities={[{
    run_id: "run-live", session_id: "session_abcdef0123456789", mission_id: "m05-work", project_id: "poincare-conjecture",
    harness: "codex", host_id: "worker",
  }]} />);
  assert.match(html, /activity-document-reference/);
  assert.match(html, /project_view=missions&amp;mission=m05-work/);
  assert.match(html, />running</);
  assert.doesNotMatch(html, /#session_/);
  const numbered = renderToStaticMarkup(<NodeSessionTags activities={[{
    run_id: "run-live", session_id: "session_abcdef0123456789", session_number: 12,
  }]} />);
  assert.match(numbered, /#12 running/);
  assert.doesNotMatch(numbered, /#session_/);
});


test("source references link to their workspace commit without rewriting Lean or external links", () => {
  const revision = "9df9083bebbe3e5c3a41129ffc3f7382c1a11104";
  const workspace = { url: "https://forge.example/geometry%20team/workspace" };
  const context = sourceContext(workspace, revision)!;
  const file = "formalized-sources/Petersen/PetersenLib/Ch03/CurvatureTensor.lean";
  const base = workspace.url;
  const source = `Source files: \`${file}\`\nSource commit: \`${revision}\`\n\n[Lines](${file}#L12-L15)\n\n[External](https://example.org/File.lean)\n\n\`\`\`lean\n-- ${file}\n\`\`\``;
  const html = renderToStaticMarkup(<BlueprintMarkdown content={source} sourceContext={context} />);
  assert(html.includes(`href="${base}/src/commit/${revision}/${file}"`));
  assert(html.includes(`href="${base}/commit/${revision}"`));
  assert(html.includes(`href="${base}/src/commit/${revision}/${file}#L12-L15"`));
  assert.match(html, /href="https:\/\/example.org\/File.lean"/);
  assert.doesNotMatch(html.match(/<pre[\s\S]*?<\/pre>/)?.[0] || "", /<a /);
  assert.equal(sourceHref(file + "#PetersenLib.curvatureTensorFour", context), `${base}/src/commit/${revision}/${file}`);
  for (const invalid of ["../secret.lean", "/home/worker/File.lean", "https://example.org/File.lean", "javascript:File.lean"]) assert.equal(sourceHref(invalid, context), undefined);
  assert.equal(sourceContext(workspace, "main"), undefined);
  assert.equal(sourceHref(file), undefined);
  assert.equal(repositoryUrl({url: "javascript:alert(1)"}), undefined);
  assert.equal(repositoryUrl({url: "https://secret:password@forge.example/workspace"}), undefined);
});


const bibtex = String.raw`
@string{journal = "Probability Surveys"}
@article{jones2004,
  author = {Jones, G. L. and de la Vall{\'e}e Poussin, Charles},
  title = {On the {Markov} chain central limit theorem},
  journal = journal,
  year = {2004},
  volume = {1},
  pages = {299--320},
  doi = {10.1214/154957804100000051}
}
@book{topping,
  title = {Lectures on {Ricci} Flow},
  author = {Topping, Peter},
  year = 2006,
  publisher = {Cambridge University Press}
}`;

test("titles render spaced inline math and document macros without block markup or HTML injection", async () => {
  const html = await renderAsync(<MathTitle title={String.raw`$ (0,4) $-curvature and $\Ric$, <img src=x>`} metadata={{ math_macros: { "\\Ric": "\\operatorname{Ric}" } }} />);
  assert.equal((html.match(/class="katex"/g) || []).length, 2);
  assert.doesNotMatch(html, /katex-error|<p>|<img|<a /);
  assert.match(html, /&lt;img src=x&gt;/);
  const plain = renderToStaticMarkup(<MathTitle title={'"PDE" as Singular and Plural'} />);
  assert.doesNotMatch(plain, /class="katex"/);
  assert.match(plain, /Singular and Plural/);
});

test("BibTeX resolves strings, nested title braces, author names and structured publication metadata", () => {
  const entries = parseBibliography(bibtex);
  assert.equal(entries.length, 2);
  assert.equal(entries[0].key, "jones2004");
  assert.equal(entries[0].title, "On the Markov chain central limit theorem");
  assert.equal(entries[0].venue, "Probability Surveys");
  assert.match(entries[0].authors, /G\. L\. Jones/);
  assert.match(entries[0].authors, /Charles de la Vallée Poussin/);
  assert.equal(entries[0].year, "2004");
  assert.equal(entries[0].url, "https://doi.org/10.1214/154957804100000051");
  assert.equal(entries[1].venue, "Cambridge University Press");
});

test("malformed or non-BibTeX source produces a visible error without rendering a raw source disclosure", () => {
  assert.throws(() => parseBibliography("@article{broken, title={Missing brace}"));
  assert.throws(() => parseBibliography("https://example.com/paper"), /No BibTeX entries/);
  const html = renderToStaticMarkup(<Bibliography bibtex="@article{broken, title={Missing brace}" />);
  assert.match(html, /role="alert"/);
  assert.match(html, /Invalid BibTeX/);
  assert.match(html, /expected/);
  assert.doesNotMatch(html, /BibTeX source|<details/);
  assert.match(html, /Missing brace/);
});

test("bibliography formatting keeps source text escaped and disallows unsafe reference URLs", () => {
  const html = renderToStaticMarkup(<Bibliography bibtex={'@misc{unsafe,title={<img src=x onerror=alert(1)>},url={javascript:alert(1)}}'} />);
  assert.doesNotMatch(html, /<img|href="javascript:/);
  assert.match(html, /&lt;img/);
  assert.equal(safeReferenceUrl("javascript:alert(1)"), undefined);
  assert.equal(safeReferenceUrl("data:text/html,test"), undefined);
  assert.equal(safeReferenceUrl("https://example.com/paper"), "https://example.com/paper");
});

test("node references ignore code, comments, images and external links and preserve first-reference order", () => {
  const markdown = [
    "A [[node:claim-a]] and [another](node:claim-b). [[node:claim-a]]",
    "`[[node:inline-example]]`",
    "```lean\n-- [[node:code-example]]\n```",
    "<!-- [[node:comment-example]] -->",
    "[External [[node:external-example]]](https://example.com/node:other)",
    "![Image [[node:image-example]]](node:fake)",
    "> [!lemma] Uses **[[node:claim-c]]**.",
  ].join("\n\n");
  assert.deepEqual(referencedNodeIds(markdown), ["claim-a", "claim-b", "claim-c"]);
});

test("mathematical callouts render inline/display math, nested content and highlighted Lean", () => {
  const html = renderToStaticMarkup(<BlueprintMarkdown content={[String.raw`> [!definition] A module $M$ over $R$ is an additive group.
>
> $$r(x+y)=rx+ry.$$

> [!proof]
> Apply the distributive law.`, "", "```lean", "theorem identity : True := by", "  trivial", "```"].join("\n")} />);
  assert.match(html, /data-callout="definition"/);
  assert.match(html, /data-callout="proof"/);
  assert.match(html, /class="katex"/);
  assert.match(html, /katex-display/);
  assert.match(html, /hl-decl/);
  assert.match(html, /hl-kw/);
  assert.doesNotMatch(html, /\[!definition\]|\[!proof\]/);
});

test("Pandoc display delimiters stay block math when prose shares their paragraph", () => {
  const html = renderToStaticMarkup(<BlueprintMarkdown content={String.raw`The converted note introduces $$\sum_{i=1}^{n} i = \frac{n(n+1)}{2}$$ before continuing the argument.`} />);
  assert.match(html, /katex-display/);
  assert.match(html, /The converted note introduces/);
  assert.match(html, /before continuing the argument/);
});

test("Markdown refuses raw HTML and unsafe links, and KaTeX cannot emit trusted links", () => {
  const html = renderToStaticMarkup(<BlueprintMarkdown content={[String.raw`<script>alert(1)</script>

[Unsafe](javascript:alert(2))

$\href{javascript:alert(3)}{danger}$`, "", "```lean", "-- <img src=x onerror=alert(4)>", "```"].join("\n")} />);
  assert.doesNotMatch(html, /<script|<img|href="javascript:/);
  assert.match(html, /&lt;img/);
});

test("unknown callout types stay ordinary quotes and node mentions link to node pages", () => {
  const html = renderToStaticMarkup(<BlueprintMarkdown content={"> [!custom] Remains prose.\n\n[[node:claim-a]]"} projectId="algebra" />);
  assert.match(html, /\[!custom\]/);
  assert.match(html, /\?project=algebra&amp;node=claim-a/);
  assert.doesNotMatch(html, /data-callout="custom"/);
});

test("proof checklists retain their checked states", () => {
  const html = renderToStaticMarkup(<BlueprintMarkdown content={"- [x] First case\n- [ ] Remaining case"} />);
  assert.match(html, /type="checkbox" disabled="" checked=""/);
  assert.match(html, /type="checkbox" disabled=""\/>/);
});

test("shared KaTeX remains compatible with existing blueprint render options and macros", () => {
  const html = katex.renderToString(String.raw`\RR^n \longrightarrow \RR`, {
    displayMode: true, throwOnError: false, strict: "ignore", trust: false,
    macros: { "\\RR": "\\mathbb{R}" },
  });
  assert.match(html, /katex-display/);
  assert.doesNotMatch(html, /katex-error/);
});

test("roadmap checklist equations render with math styles while status and node links remain intact", () => {
  const content = [
    String.raw`- [x] Establish $\pi_1(M) = 0$. [[node:claim-poincare]]`,
    String.raw`- [blocked] Show $M \cong S^3$. [Topological conclusion](node:claim-poincare)`,
    String.raw`- [in progress] Analyze $\partial_t g = -2\operatorname{Ric}(g)$.`,
    "",
    String.raw`$$\operatorname{Ric}(g) \geq 0.$$`,
    "",
    String.raw`$\href{javascript:alert(1)}{unsafe}$`,
  ].join("\n");
  const html = renderToStaticMarkup(<RoadmapMarkdown content={content} nodes={[{ id: "claim-poincare", title: "Poincare conjecture", status: "open" }]} projectId="topology" onNode={() => {}} />);
  assert.match(html, /class="katex"/);
  assert.match(html, /katex-display/);
  assert.match(html, /platform-roadmap-item done/);
  assert.match(html, /platform-roadmap-item blocked/);
  assert.match(html, /platform-roadmap-item in_progress/);
  assert.match(html, /href="\?project=topology&amp;node=claim-poincare"/);
  assert.match(html, />Poincare conjecture<\/a>/);
  assert.match(html, />Topological conclusion<\/a>/);
  assert.doesNotMatch(html, /\$\\pi_1|href="javascript:/);
  assert.match(html, /lucide-loader-circle/);
});

test("document citations resolve fenced BibTeX locally, preserve locators, and mark unknown keys", () => {
  const source = "@book{MorganTian,title={Ricci Flow and the Poincare Conjecture},author={Morgan, John W. and Tian, Gang},year={2007},publisher={American Mathematical Society}}";
  const content = ["The surgery argument is used in [[ref:MorganTian|ch11:11.1]].", "", "Missing material: [[ref:UnknownSource]].", "", "## References", "", "```bibtex", source, "```"].join("\n");
  const entries = bibliographyEntriesFromMarkdown(content);
  assert.equal(entries[0].key, "MorganTian");
  const html = renderToStaticMarkup(<BlueprintMarkdown content={content} bibliographyEntries={entries} />);
  assert.match(html, /class="platform-reference-citation" href="#reference-MorganTian"/);
  assert.match(html, />\[Morgan, 2007, ch11:11\.1\]<\/a>/);
  assert.match(html, /title="John W\. Morgan, Gang Tian\. \(2007\)\. Ricci Flow and the Poincare Conjecture\. American Mathematical Society\. Location: ch11:11\.1"/);
  assert.match(html, /platform-reference-citation unresolved/);
  assert.match(html, /\[missing: UnknownSource\]/);
  const bibliography = renderToStaticMarkup(<Bibliography bibtex={source} />);
  assert.match(bibliography, /id="reference-MorganTian"/);
});

test("document-scoped math_macros render only in their own document and reject unsafe definitions", () => {
  const withMacro = composeDocument({ title: "Ricci", math_macros: { "\\Ric": "\\operatorname{Ric}" } }, String.raw`$\Ric(g)$`);
  const withMacroHtml = renderToStaticMarkup(<DocumentView document={withMacro} />);
  assert.match(withMacroHtml, /class="katex"/);
  assert.doesNotMatch(withMacroHtml, /katex-error/);
  const withoutMacroHtml = renderToStaticMarkup(<DocumentView document={composeDocument({ title: "No macro" }, String.raw`$\Ric(g)$`)} />);
  assert.match(withoutMacroHtml, /mathcolor="#cc0000"/);
  const invalid = splitDocument(["---", "title: Unsafe", "math_macros:", "  \"\\\\R\": \"\\\\input{private.tex}\"", "---", "$\\R$"].join("\n"));
  assert.match(invalid.error || "", /Unsafe/);
});

test("YAML frontmatter round-trips nested metadata independently of its Markdown body", () => {
  const metadata = { title: "Poincare: a conjecture", type: "theorem", tags: ["Topology"], children: ["claim-a"], custom: { source: "Morgan and Tian" } };
  const body = "## Informal description\n\nA closed $3$-manifold.\n\n## References\n";
  const document = composeDocument(metadata, body);
  const parsed = splitDocument(document);
  assert.equal(parsed.error, undefined);
  assert.equal(parsed.hasFrontmatter, true);
  assert.deepEqual(parsed.metadata, metadata);
  assert.equal(parsed.body, body);
  assert.equal(splitDocument("Ordinary Markdown\n---\nbody").hasFrontmatter, false);
  assert.equal(splitDocument("---\ntitle: broken\nbody").error, "YAML frontmatter has no closing delimiter.");
  assert.match(splitDocument("---\ntitle: one\ntitle: two\n---\nBody").error || "", /unique|same key/i);
  assert.match(splitDocument("---\n- not a mapping\n---\nBody").error || "", /mapping/);
});





test("rendered documents hide frontmatter and expose only colored metadata chips and free content", () => {
  const document = composeDocument({ title: "Hidden metadata title", type: "theorem", tags: ["Topology", "Geometry"], secret_field: "not-visible", checks: { lake: "pass" } }, "> [!theorem] $M$ is a sphere.");
  const html = renderToStaticMarkup(<DocumentView document={document} />);
  assert.doesNotMatch(html, /secret_field|not-visible|title:|Hidden metadata title/);
  assert.match(html, /platform-document-tag/);
  assert.match(html, /Topology/);
  assert.match(html, /Lake ok/);
  assert.match(html, /class="katex"/);
  const tags = renderToStaticMarkup(<TagList tags={["Sketch", "Lake ok", "Lake failed"]} />);
  assert.match(tags, /tone-amber/);
  assert.match(tags, /tone-green/);
  assert.match(tags, /tone-red/);
  const withLabels = renderToStaticMarkup(<DocumentView document={composeDocument({ title: "M05", type: "theorem", labels: ["milestone", "formally_stated"], tags: ["milestone"] }, "Statement.")} />);
  assert.match(withLabels, /Theorem/);
  assert.doesNotMatch(withLabels, /Formally Stated|Milestone/);
});

test("document roadmap mode keeps statuses and node links around its References section and removes a duplicate title", () => {
  const document = composeDocument({ title: "Formalize Ricci flow", tags: ["Geometry"], bibtex }, [
    "# Objective: Formalize Ricci flow", "", "- [x] Establish $M$. [[node:claim-a]]", "- [blocked] Await comparison", "", "## References", "", "- [in progress] Review the $S^3$ conclusion.",
  ].join("\n"));
  const html = renderToStaticMarkup(<DocumentView document={document} roadmap projectId="geometry" nodes={[{ id: "claim-a", title: "Metric" }]} />);
  assert.match(html, /platform-roadmap-item done/);
  assert.match(html, /platform-roadmap-item blocked/);
  assert.match(html, /platform-roadmap-item in_progress/);
  assert.match(html, /class="katex"/);
  assert.match(html, /href="\?project=geometry&amp;node=claim-a"/);
  assert.doesNotMatch(html, /<h1>|Objective:/);
  assert.match(html, /<h2>References<\/h2>/);
  assert.match(html, /Geometry/);
});


test("multiple fenced BibTeX blocks render formatted references in prose order without pre nesting or raw source", async () => {
  const content = ["Before $M$.", "", "```bibtex", "@article{one, title={First result}, author={Jones, G. L.}, year={2004}}", "```", "", "Between.", "", "```bibtex", "@book{two, title={Second result}, author={Topping, Peter}, year={2006}}", "```", "", "After."].join("\n");
  const html = await renderAsync(<BlueprintMarkdown content={content} />);
  assert.equal(html.match(/class="platform-bibliography /g)?.length, 2);
  assert.ok(html.indexOf("Before") < html.indexOf("First result"));
  assert.ok(html.indexOf("First result") < html.indexOf("Between"));
  assert.ok(html.indexOf("Between") < html.indexOf("Second result"));
  assert.ok(html.indexOf("Second result") < html.indexOf("After"));
  assert.match(html, /class="katex"/);
  assert.doesNotMatch(html, /<pre|@article|@book|BibTeX source/);
});

test("fenced bibliography renders in roadmap lists with math and preserves literal inline code", async () => {
  const content = ["- [in progress] Check $M$.", "", "  ```bibtex", "  @article{nested, title={Nested reference}, year={2004}}", "  ```", "", "`@article{literal, title={Raw example}}`", "", "<script>alert(1)</script>"].join("\n");
  const html = await renderAsync(<RoadmapMarkdown content={content} nodes={[]} projectId="geometry" />);
  assert.match(html, /platform-roadmap-item in_progress/);
  assert.match(html, /class="katex"/);
  assert.match(html, /platform-reference-title">Nested reference/);
  assert.match(html, /<code>@article\{literal/);
  assert.doesNotMatch(html, /<pre|<script|@article\{nested/);
});
