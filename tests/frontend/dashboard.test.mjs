import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const source = readFileSync("sdlc_harness/ops/templates/dashboard.js", "utf8");
const dashboard = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);

test("maps workflow phases to stable dashboard nodes", () => {
  assert.equal(dashboard.stageForRun({ current_phase: "requirement_clarification" }), "requirement");
  assert.equal(dashboard.stageForRun({ current_phase: "design_repairing" }), "design");
  assert.equal(dashboard.stageForRun({ current_phase: "implementation_plan_validating" }), "plan");
  assert.equal(dashboard.stageForRun({ current_phase: "implementation_repair" }), "developer");
  assert.equal(dashboard.stageForRun({ current_phase: "implementation_testing" }), "verification");
  assert.equal(dashboard.stageForRun({ current_phase: "implementation_testing", status: "implementation_tested" }), "validator");
  assert.equal(dashboard.stageForRun({ current_phase: "qa_testing" }), "qa");
  assert.equal(dashboard.stageForRun({ current_phase: "code_review_approval" }), "review");
});

test("shows only API-backed actions and hides all actions while busy", () => {
  const run = { allowed_next_actions: ["approve_checkpoint", "request_change", "get_status"] };
  const schema = { actions: { approve_checkpoint: {} } };
  assert.deepEqual(dashboard.visibleActions(run, schema), ["approve_checkpoint"]);
  assert.deepEqual(dashboard.visibleActions(run, schema, true), []);
});

test("repair summary prefers observed event limits and keeps configured defaults", () => {
  const result = dashboard.repairSummary(
    { implementation_repair_iterations: 2, qa_repair_iterations: 1, quality_reports: {} },
    [{ type: "repair_iteration_started", data: { repair_phase: "implementation", limit: 5 } }],
    { workflow: { repair_defaults: { design: 2, plan: 2, implementation: 3, qa: 3 } } },
  );
  assert.deepEqual(result.map(({ id, used, limit }) => ({ id, used, limit })), [
    { id: "design", used: 0, limit: 2 },
    { id: "plan", used: 0, limit: 2 },
    { id: "implementation", used: 2, limit: 5 },
    { id: "qa", used: 1, limit: 3 },
  ]);
});

test("repair summary keeps configured limit when repair event omits it", () => {
  const result = dashboard.repairSummary(
    { implementation_repair_iterations: 4, quality_reports: {} },
    [{ type: "repair_iteration_started", data: { repair_phase: "implementation", iteration: 4 } }],
    { workflow: { repair_defaults: { design: 2, plan: 3, implementation: 4, qa: 1 } } },
  );
  const implementation = result.find(({ id }) => id === "implementation");
  assert.deepEqual(
    { used: implementation.used, limit: implementation.limit },
    { used: 4, limit: 4 },
  );
});

test("workflow graph preserves failed verification evidence during developer repair", () => {
  const schema = {
    workflow: {
      nodes: [
        { id: "requirement", label: "Requirement" },
        { id: "design", label: "Design" },
        { id: "plan", label: "Implementation Plan" },
        { id: "developer", label: "Developer" },
        { id: "verification", label: "Verification" },
        { id: "validator", label: "Validator" },
      ],
    },
  };
  const result = dashboard.workflowNodeStates({
    current_phase: "implementation_repair",
    status: "blocked",
    last_error: { error_code: "implementation_repair_limit_exceeded" },
    quality_reports: { verification: { ok: false, status: "failed" } },
  }, schema);
  assert.equal(result.find(({ node }) => node.id === "developer").nodeState, "blocked");
  assert.deepEqual(
    result.find(({ node }) => node.id === "verification"),
    { node: { id: "verification", label: "Verification" }, nodeState: "blocked", caption: "failed" },
  );
});

test("timeline merges operations and events in descending time order", () => {
  const entries = dashboard.timelineEntries(
    [{ sequence: 1, timestamp: "2026-10-18T10:00:00Z", type: "workflow_state_changed", message: "changed" }],
    [{ operation_id: "op_1", created_at: "2026-10-18T11:00:00Z", command: "continue_run", operation_status: "queued" }],
  );
  assert.equal(entries[0].id, "op_1");
  assert.equal(entries[1].id, "event-1");
});


test("config update payload contains changed editable fields only", () => {
  const sections = [{ fields: [
    { name: "COUNT", kind: "integer", value: 2, editable: true },
    { name: "ENABLED", kind: "boolean", value: false, editable: true },
    { name: "LOCKED", kind: "string", value: "os", editable: false },
    { name: "TOKEN", kind: "secret", value: "********", editable: true, secret: true },
  ] }];
  assert.deepEqual(dashboard.configUpdateValues(sections, {
    COUNT: "3",
    ENABLED: true,
    LOCKED: "changed",
    TOKEN: "",
  }), { COUNT: 3, ENABLED: true });
});

test("config update payload omits unchanged values and accepts a replaced secret", () => {
  const sections = [{ fields: [
    { name: "MODEL", kind: "string", value: "deepseek-flash", editable: true },
    { name: "TOKEN", kind: "secret", value: "********", editable: true, secret: true },
  ] }];
  assert.deepEqual(dashboard.configUpdateValues(sections, {
    MODEL: " deepseek-flash ",
    TOKEN: "new-secret",
  }), { TOKEN: "new-secret" });
  assert.equal(dashboard.configFieldDisabled({ editable: false }), true);
  assert.equal(dashboard.configFieldDisabled({ editable: true }), false);
});
