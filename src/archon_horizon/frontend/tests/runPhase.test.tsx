import assert from "node:assert/strict";
import {test} from "node:test";
import {renderToStaticMarkup} from "react-dom/server";
import RunPhase, {phaseLabel} from "../src/pipeline/RunPhase";

test("run phase labels name the work independently of lifecycle status", () => {
  assert.equal(phaseLabel({kind: "preprocessing"}), "Pre-processing");
  assert.equal(phaseLabel({kind: "formalization"}), "Main formalization");
  assert.equal(phaseLabel({kind: "postprocessing"}), "Post-processing");
  assert.equal(phaseLabel(undefined), "Not recorded");
  assert.equal(phaseLabel({kind: "custom_review"}), "custom review");
  const html = renderToStaticMarkup(<RunPhase phase={{kind: "postprocessing"}} />);
  assert.match(html, /aria-label="Phase: Post-processing"/);
  assert.match(html, /<strong>Post-processing<\/strong>/);
  assert.doesNotMatch(html, /running|active/i);
});
