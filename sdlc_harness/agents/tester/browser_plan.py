"""Harness-owned browser evidence: capture spec renderer, evidence manifest and ARIA diff (L5.4).

The LLM never writes TypeScript for browser stages. Views are typed steps; this module renders
one deterministic Playwright spec that drives them at fixed viewports and records, after every
step, a screenshot plus text evidence (ARIA snapshot, focus, computed styles, layout) that a
Validator can judge alongside the image. Nothing here interprets use-case meaning.
"""

from __future__ import annotations

import difflib
import hashlib
import json
from pathlib import Path
import re

from ...core.errors import HarnessError
from ...verification.registry import BROWSER_CAPTURE_SPEC_PATH
from ...verification.runner import BROWSER_CAPTURE_ANNOTATION_TYPE
from ..designer.graph import is_composition_wiring
from ..developer.closure_contract import stand_in_hook_lines, stand_in_import_lines
from ..developer.component_plan import ui_obligation_rows
from ..developer.visual_profile import _contrast

BROWSER_EVIDENCE_SCHEMA_VERSION = "browser_evidence_v1"
BASELINE_VIEW_ID = "V-0"
# D13 (owner, 2026-11-12): set inside the spec, so the protected playwright.config.ts is unchanged.
BROWSER_VIEWPORTS = (
    {"viewport_id": "desktop", "width": 1280, "height": 800},
    {"viewport_id": "mobile", "width": 390, "height": 844},
)
STEP_ACTIONS = ("goto", "click", "press", "fill", "wait_for")
# How long every network response of the page is held while a step with `observe` runs and is captured.
OBSERVE_HOLD_MS = 1500
STYLE_PROPERTIES = (
    "font-family",
    "font-size",
    "color",
    "background-color",
    "padding",
    "margin",
    "display",
    "position",
    "width",
)
_STEP_TEXT_KEYS = ("path", "role", "name", "text", "key", "value")
JOURNEY_PLAN_SCHEMA_VERSION = "browser_journey_plan_v1"
# J6: bounded so a capture suite stays inside the browser_e2e stage timeout and one prompt.
JOURNEY_LIMITS = {
    "max_views": 12,
    "max_steps_per_view": 15,
    "max_key_chars": 40,
    "max_path_chars": 200,
    "max_name_chars": 200,
    "max_value_chars": 500,
    "max_press_key_chars": 40,
    "max_reason_chars": 500,
}
MAX_FAILURE_ARIA_CHARS = 6000
# Playwright's AriaRole union: a role outside it can never match, so it is a contract error (J1).
ARIA_ROLES = frozenset(
    (
        "alert alertdialog application article banner blockquote button caption cell checkbox code columnheader "
        "combobox complementary contentinfo definition deletion dialog directory document emphasis feed figure form "
        "generic grid gridcell group heading img insertion link list listbox listitem log main marquee math meter "
        "menu menubar menuitem menuitemcheckbox menuitemradio navigation none note option paragraph presentation "
        "progressbar radio radiogroup region row rowgroup rowheader scrollbar search searchbox separator slider "
        "spinbutton status strong subscript superscript switch tab table tablist tabpanel term textbox time timer "
        "toolbar tooltip tree treegrid treeitem"
    ).split()
)
_STEP_REQUIRED = {
    "goto": ("path",),
    "click": ("role",),
    "fill": ("role", "value"),
    "press": ("key",),
    "wait_for": ("role",),
}
_STEP_ALLOWED = {
    "goto": {"action", "path"},
    "click": {"action", "role", "name", "text", "within", "nth", "observe"},
    "fill": {"action", "role", "name", "text", "value", "within", "nth"},
    "press": {"action", "key", "role", "name", "text", "within", "nth", "observe"},
    "wait_for": {"action", "role", "name", "text", "within", "nth"},
}
_LOCATOR_FIELDS = {"role", "name", "text", "within", "nth"}
_VIEW_FIELDS = {"key", "scenario_id", "design_node_ids", "obligation_refs", "steps"}
_VIEW_KEY = re.compile(r"^[a-z0-9][a-z0-9-]*$")
# Facts the Journey Planner needs about how the Harness runs its steps; vocabulary-free.
JOURNEY_EXECUTION_FACTS = (
    "Each view runs once at every viewport, each time in a fresh browser page; its first step is its only "
    "navigation and must be `goto` on a path of the served web app.",
    "The served page is the production build compiled with the Harness demo flag, so it renders the approved demo "
    "data; the API and the database run for real, with the approved verification stand-ins.",
    "Before each view and viewport run, the Harness restores the approved demo seed. Steps inside that run share "
    "state, while later view or viewport runs start again from the approved seed.",
    "A locator matches an ARIA role plus exactly one of: `name` for an exact accessible name, or `text` for exact "
    "visible text. An ARIA snapshot line can show element content that is not its accessible name. `within` scopes "
    "the locator to the first container with that role and accessible name; `nth` counts from 0.",
    "Ordinary steps wait up to 5 seconds for network idle before capture. A click or press may instead carry an "
    "`observe` locator for a transient state: while that step runs the Harness holds every network response of the "
    f"page for at least {OBSERVE_HOLD_MS / 1000:g} seconds, arms the locator before the action, waits up to 5 seconds "
    "for it and captures at once without network-idle settling. A state that lasts only until a server response is "
    "therefore still on screen in that capture; the held response is recorded with the next step.",
    "After each step the Harness records a screenshot, the ARIA snapshot, the focused element, computed styles and "
    "layout. Text evidence is recorded before the slower screenshot. The record of a step with `observe` also "
    "carries `observe.status` (`observed`, or `missed` with the locator error) and `observe.network_held` (whether "
    "responses were really held).",
    "A step whose action cannot run (no match, timeout) is recorded as step_failed with its error and the ARIA "
    "snapshot at that point, and the view stops there. A missed `observe` does not stop the view: the action ran, so "
    "the step is recorded as passed and the following steps still run.",
)
_EXCLUDED_EXPERIENCE_FIELDS = {
    "visual_contract", "guidance_refs", "repo_evidence_refs", "schema_version",
    # ui_obligation_rows projects these into obligations (summary + contract), so they are not sent twice.
    "runtime_states", "feedback", "accessibility", "focus_flow", "responsive", "focus_policies",
}
_ARIA_LINE = re.compile(r'^\s*-\s+([A-Za-z]+)(?:\s+"((?:[^"\\]|\\.)*)")?')
_RGB = re.compile(r"^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([0-9.]+)\s*)?\)$")
_PLACEHOLDER = re.compile(r"__[A-Z_]+__")

_SPEC_TEMPLATE = r"""// Generated by SDLC Harness for browser evidence capture. Do not edit by hand.
import { execFileSync } from "node:child_process";
import { test, type CDPSession, type Locator, type Page, type Request, type TestInfo } from "@playwright/test";
import config from "__CONFIG_IMPORT__";
__STAND_IN_IMPORTS__
type CaptureServer = { name?: string; url?: string };
type CaptureHandle = { close(): Promise<void> };
type AriaRole = Parameters<Page["getByRole"]>[0];
type CaptureLocator = {
  role: string;
  name?: string;
  text?: string;
  within?: { role: string; name: string };
  nth?: number;
};
type CaptureStep = {
  action: "goto" | "click" | "press" | "fill" | "wait_for";
  path?: string;
  role?: string;
  name?: string;
  text?: string;
  within?: { role: string; name: string };
  nth?: number;
  key?: string;
  value?: string;
  observe?: CaptureLocator;
};
type CaptureView = { view_id: string; steps: CaptureStep[] };
type CaptureObserve = { status: "observed" | "missed"; error: string | null; network_held: boolean };
type CaptureViewport = { viewport_id: string; width: number; height: number };
type CaptureStyle = { target: string; props: Record<string, string> };
type CaptureDialog = {
  role: string;
  aria: string;
  bbox: { x: number; y: number; width: number; height: number } | null;
};
type CaptureNetworkEvent = {
  kind: "response" | "request_failed";
  method: string;
  path: string;
  cross_origin: boolean;
  request_header_names?: string[];
  status?: number;
  error?: string;
};

const VIEWS: CaptureView[] = __VIEWS__;
const VIEWPORTS: CaptureViewport[] = __VIEWPORTS__;
const STYLE_PROPERTIES: string[] = __STYLE_PROPERTIES__;
const STEP_TIMEOUT_MS = 5_000;
const NETWORK_QUIET_MS = 500;
const OBSERVE_HOLD_MS = __OBSERVE_HOLD_MS__;
const NAVIGATION_TIMEOUT_MS = 30_000;
const TEST_BASE_TIMEOUT_MS = 30_000;
const TEST_STEP_TIMEOUT_MS = 45_000;
const DATABASE_RESET_TIMEOUT_MS = 120_000;
const MAX_ARIA_SNAPSHOT_CHARS = 20_000;
const MAX_STEP_ERROR_CHARS = 2_000;
const MAX_NETWORK_EVENTS_PER_STEP = 50;

const webServers = ([] as CaptureServer[]).concat((config.webServer ?? []) as CaptureServer | CaptureServer[]);
const standIns: CaptureHandle[] = [];

test.describe.configure({ mode: "serial" });

function resetDatabase(): void {
  const executable = process.platform === "win32" ? (process.env.ComSpec ?? "cmd.exe") : "npm";
  const args = process.platform === "win32" ? ["/d", "/s", "/c", "npm run db:seed"] : ["run", "db:seed"];
  execFileSync(executable, args, {
    cwd: process.cwd(),
    env: process.env,
    stdio: "pipe",
    timeout: DATABASE_RESET_TIMEOUT_MS,
    windowsHide: true,
  });
}

function serviceOrigin(name: string): string {
  const server = webServers.find((item) => item.name === name);
  if (!server || !server.url) {
    throw new Error(`playwright.config.ts declares no webServer named ${name}`);
  }
  return new URL(server.url).origin;
}
__STAND_IN_PORT__
function exactText(value: string): RegExp {
  const escaped = value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&").replace(/\s+/g, "\\s+");
  return new RegExp(`^\\s*${escaped}\\s*$`);
}

function locate(page: Page, locator: CaptureLocator | CaptureStep): Locator {
  if (locator.role === undefined) {
    throw new Error("Capture locator requires role");
  }
  const scope: Page | Locator = locator.within
    ? page.getByRole(locator.within.role as AriaRole, { name: locator.within.name, exact: true })
    : page;
  const target = locator.name === undefined
    ? scope.getByRole(locator.role as AriaRole).filter({ hasText: exactText(locator.text ?? "") })
    : scope.getByRole(locator.role as AriaRole, { name: locator.name, exact: true });
  return locator.nth === undefined ? target : target.nth(locator.nth);
}

async function runStep(page: Page, step: CaptureStep): Promise<Locator | null> {
  switch (step.action) {
    case "goto":
      await page.goto(`${serviceOrigin("web")}${step.path ?? "/"}`, { timeout: NAVIGATION_TIMEOUT_MS });
      return null;
    case "click": {
      const target = locate(page, step);
      await target.click({ timeout: STEP_TIMEOUT_MS });
      return target;
    }
    case "fill": {
      const target = locate(page, step);
      await target.fill(step.value ?? "", { timeout: STEP_TIMEOUT_MS });
      return target;
    }
    case "press": {
      if (step.role === undefined) {
        await page.keyboard.press(step.key ?? "");
        return null;
      }
      const target = locate(page, step);
      await target.press(step.key ?? "", { timeout: STEP_TIMEOUT_MS });
      return target;
    }
    case "wait_for": {
      const target = locate(page, step);
      await target.waitFor({ state: "visible", timeout: STEP_TIMEOUT_MS });
      return target;
    }
    default:
      throw new Error(`Unsupported capture step: ${String((step as { action?: unknown }).action)}`);
  }
}

function trackInFlight(page: Page): Set<Request> {
  const inFlight = new Set<Request>();
  page.on("request", (request) => inFlight.add(request));
  page.on("requestfinished", (request) => inFlight.delete(request));
  page.on("requestfailed", (request) => inFlight.delete(request));
  return inFlight;
}

async function settle(page: Page, inFlight: Set<Request>): Promise<void> {
  // Bounded: a page that keeps polling never reaches network idle, and the capture still proceeds.
  const deadline = Date.now() + STEP_TIMEOUT_MS;
  await page.waitForLoadState("networkidle", { timeout: STEP_TIMEOUT_MS }).catch(() => undefined);
  // That load state stays reached after the first load, so it never waits for a request a later step
  // starts: also wait until no request has been in flight for NETWORK_QUIET_MS.
  let quietSince = Date.now();
  while (Date.now() < deadline) {
    if (inFlight.size > 0) {
      quietSince = Date.now();
    } else if (Date.now() - quietSince >= NETWORK_QUIET_MS) {
      return;
    }
    await page.waitForTimeout(50);
  }
}

// Throttling, not page.route: with request interception Playwright answers CORS preflights itself.
async function holdResponses(session: CDPSession | null, latencyMs: number): Promise<boolean> {
  if (session === null) {
    return false;
  }
  try {
    await session.send("Network.emulateNetworkConditions", {
      offline: false,
      latency: latencyMs,
      downloadThroughput: -1,
      uploadThroughput: -1,
    });
    return true;
  } catch {
    return false;
  }
}

function observeNetwork(page: Page, events: CaptureNetworkEvent[]): void {
  const record = (
    kind: CaptureNetworkEvent["kind"],
    method: string,
    rawUrl: string,
    requestHeaderNames: string[],
    status?: number,
    error?: string,
  ): void => {
    let parsed: URL;
    try {
      parsed = new URL(rawUrl);
    } catch {
      return;
    }
    events.push({
      kind,
      method,
      path: parsed.pathname,
      cross_origin: parsed.origin !== serviceOrigin("web"),
      ...(requestHeaderNames.length === 0 ? {} : { request_header_names: requestHeaderNames }),
      ...(status === undefined ? {} : { status }),
      ...(error ? { error: error.slice(0, MAX_STEP_ERROR_CHARS) } : {}),
    });
  };
  page.on("response", (response) => {
    const request = response.request();
    if (["document", "fetch", "xhr"].includes(request.resourceType())) {
      record(
        "response",
        request.method(),
        response.url(),
        Object.keys(request.headers()).sort(),
        response.status(),
      );
    }
  });
  page.on("requestfailed", (request) => {
    if (["document", "fetch", "xhr"].includes(request.resourceType())) {
      record(
        "request_failed",
        request.method(),
        request.url(),
        Object.keys(request.headers()).sort(),
        undefined,
        request.failure()?.errorText,
      );
    }
  });
}

// Runs in the page: it must stay self-contained.
function readStyles(element: Element, properties: string[]): Record<string, string> {
  const style = getComputedStyle(element);
  const values: Record<string, string> = {};
  for (const property of properties) {
    values[property] = style.getPropertyValue(property);
  }
  let background = "";
  for (let node: Element | null = element; node !== null && background === ""; node = node.parentElement) {
    const color = getComputedStyle(node).backgroundColor;
    if (color !== "transparent" && !/^rgba\(.*,\s*0\)$/.test(color)) {
      background = color;
    }
  }
  values["effective-background-color"] = background || "rgb(255, 255, 255)";
  return values;
}

// Runs in the page: it must stay self-contained.
function readFocus(element: Element): Record<string, string> {
  const style = getComputedStyle(element);
  return {
    tag: element.tagName.toLowerCase(),
    outline_style: style.outlineStyle,
    outline_width: style.outlineWidth,
    outline_color: style.outlineColor,
  };
}

async function capture(
  page: Page,
  testInfo: TestInfo,
  view: CaptureView,
  viewport: CaptureViewport,
  step: number,
  status: "passed" | "step_failed",
  error: string | null,
  target: Locator | null,
  network: CaptureNetworkEvent[],
  observe: CaptureObserve | null,
): Promise<void> {
  const attachment = `capture:${view.view_id}:${viewport.viewport_id}:${step}`;
  const file = testInfo.outputPath(`${view.view_id}-${viewport.viewport_id}-${step}.png`);
  // Text is authoritative; retain an observed transient before the slower screenshot work.
  const aria = await page.locator("body").ariaSnapshot();
  await page.screenshot({ path: file, fullPage: true });
  await testInfo.attach(attachment, { path: file, contentType: "image/png" });
  const focused = page.locator("*:focus");
  const hasFocus = (await focused.count()) > 0;
  const focus = hasFocus
    ? {
        aria: (await focused.first().ariaSnapshot()).split("\n")[0],
        ...(await focused.first().evaluate(readFocus)),
      }
    : null;
  const layout = await page.evaluate(() => ({
    viewport: { width: window.innerWidth, height: window.innerHeight },
    scroll_width: document.documentElement.scrollWidth,
    horizontal_overflow: document.documentElement.scrollWidth > window.innerWidth,
  }));
  const dialogs: CaptureDialog[] = [];
  for (const role of ["dialog", "alertdialog"] as const) {
    for (const dialog of await page.getByRole(role).all()) {
      dialogs.push({ role, aria: (await dialog.ariaSnapshot()).split("\n")[0], bbox: await dialog.boundingBox() });
    }
  }
  const styles: CaptureStyle[] = [
    { target: "body", props: await page.locator("body").evaluate(readStyles, STYLE_PROPERTIES) },
  ];
  if (hasFocus) {
    styles.push({ target: "focused", props: await focused.first().evaluate(readStyles, STYLE_PROPERTIES) });
  }
  if (target !== null && (await target.count()) > 0) {
    styles.push({ target: "step_locator", props: await target.first().evaluate(readStyles, STYLE_PROPERTIES) });
  }
  testInfo.annotations.push({
    type: "__ANNOTATION_TYPE__",
    description: JSON.stringify({
      view_id: view.view_id,
      viewport_id: viewport.viewport_id,
      step,
      status,
      error,
      ...(observe === null ? {} : { observe }),
      attachment,
      aria_snapshot: aria.slice(0, MAX_ARIA_SNAPSHOT_CHARS),
      aria_truncated: aria.length > MAX_ARIA_SNAPSHOT_CHARS,
      focus,
      layout: { ...layout, dialogs },
      styles,
      network: network.slice(-MAX_NETWORK_EVENTS_PER_STEP),
      network_truncated: network.length > MAX_NETWORK_EVENTS_PER_STEP,
    }),
  });
}
__STAND_IN_HOOKS__
for (const view of VIEWS) {
  for (const viewport of VIEWPORTS) {
    test(`${view.view_id}@${viewport.viewport_id}`, async ({ page }, testInfo) => {
      test.setTimeout(DATABASE_RESET_TIMEOUT_MS + TEST_BASE_TIMEOUT_MS + view.steps.length * TEST_STEP_TIMEOUT_MS);
      resetDatabase();
      await page.setViewportSize({ width: viewport.width, height: viewport.height });
      const networkEvents: CaptureNetworkEvent[] = [];
      let networkCursor = 0;
      observeNetwork(page, networkEvents);
      const inFlight = trackInFlight(page);
      const session = await page.context().newCDPSession(page).catch(() => null);
      for (let index = 0; index < view.steps.length; index += 1) {
        const step = view.steps[index];
        // A transient state lasts until its response arrives: hold responses so the capture below still shows it.
        const held = step.observe !== undefined && (await holdResponses(session, OBSERVE_HOLD_MS));
        // Armed before the action, so a state that is gone when the action returns is still seen.
        const observing = step.observe === undefined
          ? null
          : locate(page, step.observe)
              .waitFor({ state: "visible", timeout: STEP_TIMEOUT_MS })
              .then(() => null, (error: unknown) => String(error).slice(0, MAX_STEP_ERROR_CHARS));
        let target: Locator | null = null;
        let failure: string | null = null;
        try {
          target = await runStep(page, step);
        } catch (error) {
          failure = String(error).slice(0, MAX_STEP_ERROR_CHARS);
        }
        let observe: CaptureObserve | null = null;
        if (failure === null && observing === null) {
          await settle(page, inFlight);
        } else if (failure === null && observing !== null) {
          // A missed observe is evidence, not a step failure: the action ran and the view goes on.
          const missed = await observing;
          observe = { status: missed === null ? "observed" : "missed", error: missed, network_held: held };
        }
        const network = networkEvents.slice(networkCursor);
        networkCursor = networkEvents.length;
        // A step that cannot run is evidence for the journey/QA review, not a stage failure.
        await capture(page, testInfo, view, viewport, index, failure === null ? "passed" : "step_failed", failure, target, network, observe);
        if (held) {
          await holdResponses(session, 0);
        }
        if (failure !== null) {
          return;
        }
      }
    });
  }
}
"""

_STAND_IN_PORT = """
function standInPort(envKey: string): number {
  const value = process.env[envKey];
  if (!value) {
    throw new Error(`Runtime closure stand-in env ${envKey} is not set`);
  }
  return Number(new URL(value).port);
}
"""


def baseline_view() -> dict[str, object]:
    """V-0: open the web root; Harness-owned, needs no journey plan (readiness R4, journey input)."""

    return {"view_id": BASELINE_VIEW_ID, "steps": [{"action": "goto", "path": "/"}]}


def render_browser_capture_spec(
    views: list[dict[str, object]],
    *,
    stand_ins: list[dict[str, object]],
    viewports: tuple[dict[str, object], ...] = BROWSER_VIEWPORTS,
) -> str:
    """Deterministic Playwright spec driving typed views at fixed viewports (D12: no LLM TypeScript)."""

    spec_depth = len(BROWSER_CAPTURE_SPEC_PATH.split("/")) - 1
    stand_in_imports = stand_in_import_lines(stand_ins, spec_path=BROWSER_CAPTURE_SPEC_PATH)
    replacements = {
        "__CONFIG_IMPORT__": "../" * spec_depth + "playwright.config",
        "__STAND_IN_IMPORTS__": "\n".join(stand_in_imports) + "\n" if stand_in_imports else "",
        "__VIEWS__": json.dumps([capture_view(view) for view in views], ensure_ascii=False, sort_keys=True),
        "__VIEWPORTS__": json.dumps(
            [
                {"viewport_id": str(item["viewport_id"]), "width": int(item["width"]), "height": int(item["height"])}
                for item in viewports
            ],
            sort_keys=True,
        ),
        "__STYLE_PROPERTIES__": json.dumps(list(STYLE_PROPERTIES)),
        "__OBSERVE_HOLD_MS__": str(OBSERVE_HOLD_MS),
        "__ANNOTATION_TYPE__": BROWSER_CAPTURE_ANNOTATION_TYPE,
        "__STAND_IN_PORT__": _STAND_IN_PORT if stand_ins else "",
        "__STAND_IN_HOOKS__": "\n".join(stand_in_hook_lines(stand_ins)) + "\n" if stand_ins else "",
    }
    # One pass over the template: substituted values (step names) are never rescanned for placeholders.
    return _PLACEHOLDER.sub(lambda match: replacements[match.group(0)], _SPEC_TEMPLATE)


def capture_view(view: dict[str, object]) -> dict[str, object]:
    """The renderable shape of one typed view: known step fields only, unknown actions fail closed."""

    view_id = str(view.get("view_id") or "")
    steps = view.get("steps")
    if not view_id or not isinstance(steps, list) or not steps:
        raise HarnessError("browser_capture_view_invalid", f"Browser capture view needs an id and steps: {view_id!r}")
    rendered: list[dict[str, object]] = []
    for step in steps:
        action = step.get("action") if isinstance(step, dict) else None
        if action not in STEP_ACTIONS:
            raise HarnessError("browser_capture_view_invalid", f"View {view_id} has an unsupported step action: {action!r}")
        item: dict[str, object] = {"action": action}
        item.update({key: step[key] for key in _STEP_TEXT_KEYS if isinstance(step.get(key), str)})
        if isinstance(step.get("nth"), int) and not isinstance(step.get("nth"), bool):
            item["nth"] = step["nth"]
        within = step.get("within")
        if isinstance(within, dict) and isinstance(within.get("role"), str) and isinstance(within.get("name"), str):
            item["within"] = {"role": within["role"], "name": within["name"]}
        observe = step.get("observe")
        if isinstance(observe, dict):
            rendered_observe = {
                key: observe[key]
                for key in ("role", "name", "text")
                if isinstance(observe.get(key), str)
            }
            if isinstance(observe.get("nth"), int) and not isinstance(observe.get("nth"), bool):
                rendered_observe["nth"] = observe["nth"]
            observe_within = observe.get("within")
            if (
                isinstance(observe_within, dict)
                and isinstance(observe_within.get("role"), str)
                and isinstance(observe_within.get("name"), str)
            ):
                rendered_observe["within"] = {
                    "role": observe_within["role"],
                    "name": observe_within["name"],
                }
            item["observe"] = rendered_observe
        rendered.append(item)
    return {"view_id": view_id, "steps": rendered}


def build_browser_evidence_manifest(
    *,
    views: list[dict[str, object]],
    capture_results: list[dict[str, object]] | tuple[dict[str, object], ...],
    viewports: tuple[dict[str, object], ...] = BROWSER_VIEWPORTS,
    evidence_dir: str = "",
) -> dict[str, object]:
    """browser_evidence_v1: one record per (view, step, viewport); numbers are evidence, never gates."""

    view_order = {str(view.get("view_id")): index for index, view in enumerate(views)}
    viewport_order = {str(item["viewport_id"]): index for index, item in enumerate(viewports)}
    records = sorted(
        (dict(item) for item in capture_results),
        key=lambda item: (
            view_order.get(str(item.get("view_id")), len(view_order)),
            viewport_order.get(str(item.get("viewport_id")), len(viewport_order)),
            int(item.get("step") or 0),
        ),
    )
    evidence = [_evidence_record(item) for item in records]
    return {
        "schema_version": BROWSER_EVIDENCE_SCHEMA_VERSION,
        "evidence_dir": evidence_dir,
        "views": [capture_view(view) for view in views],
        "viewports": [dict(item) for item in viewports],
        "records": evidence,
        "summary": {
            "captures": len(evidence),
            "step_failed": [
                {key: item[key] for key in ("view_id", "viewport_id", "step")}
                for item in evidence
                if item["status"] == "step_failed"
            ],
            "observe_missed": [
                {key: item[key] for key in ("view_id", "viewport_id", "step")}
                for item in evidence
                if (item.get("observe") or {}).get("status") == "missed"
            ],
        },
    }


def select_primary_images(
    capture_sets: list[dict[str, object]],
    *,
    run_dir: Path,
    max_images: int,
    include_baseline: bool = True,
) -> dict[str, object]:
    """D23: last capture per view/viewport, ordered and capped without splitting before/after pairs."""

    if not capture_sets or max_images < 1:
        raise HarnessError("browser_image_selection_invalid", "Image selection needs capture sets and a positive limit.")
    normalized: list[tuple[str, dict[str, object], dict[tuple[str, str], dict[str, object]]]] = []
    seen_variants: set[str] = set()
    for item in capture_sets:
        variant = str(item.get("variant") or "").strip()
        manifest = item.get("manifest")
        if not variant or variant in seen_variants or not isinstance(manifest, dict):
            raise HarnessError("browser_image_selection_invalid", "Capture-set variants must be unique and non-empty.")
        seen_variants.add(variant)
        primary: dict[tuple[str, str], dict[str, object]] = {}
        for record in _items(manifest.get("records")):
            key = (str(record.get("view_id") or ""), str(record.get("viewport_id") or ""))
            if not all(key):
                continue
            current = primary.get(key)
            if current is None or int(record.get("step") or 0) > int(current.get("step") or 0):
                primary[key] = record
        normalized.append((variant, manifest, primary))

    first_manifest = normalized[0][1]
    view_ids = [str(item.get("view_id") or "") for item in _items(first_manifest.get("views"))]
    view_ids = (
        [BASELINE_VIEW_ID, *(item for item in view_ids if item and item != BASELINE_VIEW_ID)]
        if include_baseline
        else [item for item in view_ids if item and item != BASELINE_VIEW_ID]
    )
    viewport_ids = [str(item.get("viewport_id") or "") for item in _items(first_manifest.get("viewports"))]
    viewport_ids = [
        *(item for item in ("desktop", "mobile") if item in viewport_ids),
        *(item for item in viewport_ids if item and item not in {"desktop", "mobile"}),
    ]
    if not view_ids or not viewport_ids:
        raise HarnessError("browser_image_selection_invalid", "The evidence manifest has no ordered views or viewports.")

    selected: list[dict[str, object]] = []
    omitted: list[str] = []
    for view_id in view_ids:
        for viewport_id in viewport_ids:
            capture_key = f"{view_id}:{viewport_id}"
            group = [
                _image_reference(
                    variant=variant,
                    manifest=manifest,
                    record=primary.get((view_id, viewport_id)),
                    run_dir=run_dir,
                    capture_key=capture_key,
                )
                for variant, manifest, primary in normalized
            ]
            if len(selected) + len(group) <= max_images:
                selected.extend(group)
            else:
                omitted.append(capture_key)
    return {"images": selected, "omitted_capture_keys": omitted}


def aria_diff(before: dict[str, object], after: dict[str, object]) -> list[dict[str, object]]:
    """Per-capture text diff of ARIA snapshots before/after a change; the Validator judges meaning (D9)."""

    def index(manifest: dict[str, object]) -> dict[tuple[str, int, str], str]:
        records = manifest.get("records") if isinstance(manifest, dict) else None
        return {
            (str(item.get("view_id")), int(item.get("step") or 0), str(item.get("viewport_id"))): str(
                item.get("aria_snapshot") or ""
            )
            for item in records or []
            if isinstance(item, dict)
        }

    old, new = index(before), index(after)
    diffs: list[dict[str, object]] = []
    for key in [*old, *(key for key in new if key not in old)]:
        if key not in new:
            status, lines = "removed", []
        elif key not in old:
            status, lines = "added", []
        elif old[key] == new[key]:
            status, lines = "unchanged", []
        else:
            status = "changed"
            lines = [
                line
                for line in difflib.unified_diff(old[key].splitlines(), new[key].splitlines(), lineterm="", n=0)
                if not line.startswith(("---", "+++", "@@"))
            ]
        diffs.append({"view_id": key[0], "step": key[1], "viewport_id": key[2], "status": status, "diff": lines})
    return diffs


# ---------------------------------------------------------------------- journey plan (L5.4.2)


def obligation_catalog(
    *,
    frozen_packet: dict[str, object],
    node_scenarios: dict[str, object],
    runnable_scenarios: set[str],
) -> list[dict[str, object]]:
    """Browser-observable UI obligations of the frozen Plan whose Design node serves a runnable scenario.

    An entry carries `contract`: the fields of the approved Design item that its summary leaves out (what must be
    seen). Obligations of a composition-wiring node are left out: it forwards inputs and renders nothing itself.
    """

    catalog: list[dict[str, object]] = []
    seen: set[str] = set()
    for unit in _items(frozen_packet.get("execution_units")):
        nodes = {
            str(surface.get("design_node_id") or ""): {
                "id": surface.get("design_node_id"),
                "surface": surface.get("surface"),
                "surface_contract": surface.get("surface_contract"),
            }
            for surface in _items(unit.get("surface_contracts"))
        }
        contracts = {
            row_id: detail for node in nodes.values() for row_id, _summary, detail in ui_obligation_rows(node)
        }
        for item in _items(unit.get("ui_obligations")):
            obligation_id = str(item.get("id") or "").strip()
            node_id = obligation_id.partition(":")[0]
            if not obligation_id or obligation_id in seen or is_composition_wiring(nodes.get(node_id, {})):
                continue
            listed = node_scenarios.get(node_id)
            scenarios = sorted(str(value) for value in listed) if isinstance(listed, list) and listed else (
                [str(unit["scenario_id"])] if unit.get("scenario_id") else []
            )
            if not set(scenarios) & runnable_scenarios:
                continue
            seen.add(obligation_id)
            catalog.append(
                {
                    "obligation_id": obligation_id,
                    "summary": str(item.get("summary") or ""),
                    **({"contract": contracts[obligation_id]} if contracts.get(obligation_id) else {}),
                    "design_node_id": node_id,
                    "scenario_ids": scenarios,
                    "unit_id": str(unit.get("unit_id") or ""),
                }
            )
    return catalog


def requirement_scenarios(
    *,
    frozen_packet: dict[str, object],
    requirement_model: dict[str, object],
    runnable_scenarios: list[str],
) -> list[dict[str, object]]:
    """What each runnable scenario means in the approved Requirement: name, kind and the text of its cited items."""

    text = {str(item.get("id") or ""): item for item in _items(frozen_packet.get("canonical_requirements"))}
    contract = requirement_model.get("scenario_contract")
    scenarios: list[dict[str, object]] = []
    for scenario in _items(contract.get("scenarios") if isinstance(contract, dict) else None):
        scenario_id = str(scenario.get("id") or "")
        if scenario_id not in runnable_scenarios:
            continue
        refs = [str(ref) for key in ("requirement_refs", "acceptance_refs") for ref in scenario.get(key) or []]
        scenarios.append(
            {
                "scenario_id": scenario_id,
                "name": scenario.get("name"),
                "kind": scenario.get("kind"),
                "requirements": [
                    {"id": ref, "section": text[ref].get("section"), "content": text[ref].get("content")}
                    for ref in refs
                    if ref in text
                ],
            }
        )
    return scenarios


def experience_contracts(frozen_packet: dict[str, object], node_ids: set[str]) -> list[dict[str, object]]:
    """Screen and component structure of each node; behavior items travel as obligations, visuals are not needed."""

    contracts: dict[str, dict[str, object]] = {}
    for unit in _items(frozen_packet.get("execution_units")):
        for surface in _items(unit.get("surface_contracts")):
            node_id = str(surface.get("design_node_id") or "")
            contract = (surface.get("surface_contract") or {}).get("experience_contract")
            if node_id in node_ids and isinstance(contract, dict) and node_id not in contracts:
                kept = {key: value for key, value in contract.items() if key not in _EXCLUDED_EXPERIENCE_FIELDS}
                if kept:
                    contracts[node_id] = {"design_node_id": node_id, **kept}
    return [contracts[node_id] for node_id in sorted(contracts)]


def scenario_design_nodes(
    *,
    node_scenarios: dict[str, object],
    catalog: list[dict[str, object]],
    runnable_scenarios: set[str],
) -> dict[str, list[str]]:
    """Design nodes serving each runnable scenario, from the same sources gate J2 uses."""

    nodes: dict[str, set[str]] = {scenario_id: set() for scenario_id in runnable_scenarios}
    for node_id, scenarios in node_scenarios.items():
        for scenario_id in scenarios if isinstance(scenarios, list) else []:
            if str(scenario_id) in nodes:
                nodes[str(scenario_id)].add(str(node_id))
    for item in catalog:
        if str(item["design_node_id"]) not in node_scenarios:
            for scenario_id in item["scenario_ids"]:
                if scenario_id in nodes:
                    nodes[scenario_id].add(str(item["design_node_id"]))
    return {scenario_id: sorted(values) for scenario_id, values in sorted(nodes.items())}


def journey_planner_view(
    *,
    runnable_scenarios: list[str],
    scenarios: list[dict[str, object]],
    scenario_nodes: dict[str, list[str]],
    catalog: list[dict[str, object]],
    contracts: list[dict[str, object]],
    baseline_aria: dict[str, str],
    ui_sources: list[dict[str, object]],
    demo_data: dict[str, object],
) -> dict[str, object]:
    """Everything the Journey Planner reads; text only (locators need exact accessible names)."""

    step_actions: dict[str, dict[str, object]] = {}
    for action in STEP_ACTIONS:
        contract: dict[str, object] = {
            "required": list(_STEP_REQUIRED[action]),
            "optional": sorted(_STEP_ALLOWED[action] - {"action", *_STEP_REQUIRED[action]}),
        }
        if action in {"click", "fill", "wait_for"}:
            contract["exactly_one_of"] = [["name", "text"]]
        elif action == "press":
            contract["optional_locator"] = {
                "required": ["role"],
                "exactly_one_of": [["name", "text"]],
            }
        if action in {"click", "press"}:
            contract["observe"] = {
                "required": ["role"],
                "exactly_one_of": [["name", "text"]],
                "optional": ["within", "nth"],
            }
        step_actions[action] = contract

    return {
        "runnable_scenario_ids": list(runnable_scenarios),
        "scenarios": scenarios,
        "scenario_design_nodes": dict(scenario_nodes),
        "obligations": [
            {
                key: item[key]
                for key in ("obligation_id", "summary", "contract", "design_node_id", "scenario_ids")
                if key in item
            }
            for item in catalog
        ],
        "experience_contracts": contracts,
        "baseline_view": {"view_id": BASELINE_VIEW_ID, "steps": baseline_view()["steps"]},
        "baseline_aria_snapshot": dict(baseline_aria),
        "ui_sources": ui_sources,
        "demo_data": demo_data,
        "viewports": [dict(item) for item in BROWSER_VIEWPORTS],
        "step_actions": step_actions,
        "aria_roles": sorted(ARIA_ROLES),
        "limits": dict(JOURNEY_LIMITS),
        "execution_facts": list(JOURNEY_EXECUTION_FACTS),
    }


def journey_plan_rule_issues(
    output: dict[str, object],
    *,
    catalog: list[dict[str, object]],
    node_scenarios: dict[str, object],
    runnable_scenarios: set[str],
) -> list[dict[str, object]]:
    """Gates J1-J6 on any typed plan; structure and IDs only, never what a step means."""

    issues: list[dict[str, object]] = []

    def issue(gate: str, message: str) -> None:
        issues.append({"gate": gate, "message": message})

    limits = JOURNEY_LIMITS
    obligation_node = {str(item["obligation_id"]): str(item["design_node_id"]) for item in catalog}
    # Same scenario source as the catalog: the Design map, else the owning Plan unit's scenario.
    catalog_scenarios: dict[str, set[str]] = {}
    for item in catalog:
        catalog_scenarios.setdefault(str(item["design_node_id"]), set()).update(item["scenario_ids"])

    def served_by(node_id: str) -> set[str]:
        listed = node_scenarios.get(node_id)
        return {str(value) for value in listed} if isinstance(listed, list) and listed else catalog_scenarios.get(node_id, set())

    views = output.get("views")
    if not isinstance(views, list) or not all(isinstance(item, dict) for item in views):
        issue("J1", "`views` must be a list of objects.")
        views = []
    if len(views) > limits["max_views"]:
        issue("J6", f"At most {limits['max_views']} views are allowed; got {len(views)}.")
    covered: dict[str, list[str]] = {}
    keys: list[str] = []
    for index, view in enumerate(views, start=1):
        key = str(view.get("key") or "")
        label = f"view {index} `{key}`"
        unknown = sorted(set(view) - _VIEW_FIELDS)
        if unknown:
            issue("J1", f"{label} has unknown fields {unknown}.")
        if not _VIEW_KEY.fullmatch(key) or len(key) > limits["max_key_chars"]:
            issue("J1", f"{label}: `key` must be lowercase kebab-case of at most {limits['max_key_chars']} characters.")
        elif key in keys:
            issue("J1", f"{label}: `key` repeats an earlier view.")
        keys.append(key)
        scenario_id = str(view.get("scenario_id") or "")
        if scenario_id not in runnable_scenarios:
            issue("J2", f"{label}: scenario `{scenario_id}` is not a runnable scenario {sorted(runnable_scenarios)}.")
        nodes = view.get("design_node_ids")
        if not isinstance(nodes, list) or not nodes or not all(isinstance(item, str) for item in nodes):
            issue("J1", f"{label}: `design_node_ids` must be a non-empty list of strings.")
            nodes = []
        for node_id in nodes:
            if scenario_id not in served_by(node_id):
                issue("J2", f"{label}: Design node `{node_id}` does not serve scenario `{scenario_id}`.")
        refs = view.get("obligation_refs")
        if not isinstance(refs, list) or not all(isinstance(item, str) for item in refs):
            issue("J1", f"{label}: `obligation_refs` must be a list of strings.")
            refs = []
        for ref in refs:
            if ref not in obligation_node:
                issue("J3", f"{label}: obligation `{ref}` is not in the obligation catalog.")
            elif obligation_node[ref] not in nodes:
                issue("J3", f"{label}: obligation `{ref}` belongs to node `{obligation_node[ref]}`, which the view does not declare.")
            else:
                covered.setdefault(ref, []).append(key)
        steps = view.get("steps")
        if not isinstance(steps, list) or not steps:
            issue("J1", f"{label}: `steps` must be a non-empty list.")
            continue
        if len(steps) > limits["max_steps_per_view"]:
            issue("J6", f"{label}: at most {limits['max_steps_per_view']} steps are allowed; got {len(steps)}.")
        for number, step in enumerate(steps, start=1):
            issues.extend(
                {"gate": gate, "message": f"{label} step {number}: {message}"}
                for gate, message in _step_issues(step, first=number == 1)
            )
    not_reachable = output.get("not_reachable", [])
    if not isinstance(not_reachable, list) or not all(isinstance(item, dict) for item in not_reachable):
        issue("J1", "`not_reachable` must be a list of objects.")
        not_reachable = []
    unreachable: set[str] = set()
    for item in not_reachable:
        obligation_id = str(item.get("obligation_id") or "")
        reason = str(item.get("reason") or "").strip()
        if obligation_id not in obligation_node:
            issue("J3", f"not_reachable names `{obligation_id}`, which is not in the obligation catalog.")
        elif obligation_id in covered:
            issue("J4", f"`{obligation_id}` is both observed by {covered[obligation_id]} and listed as not_reachable.")
        if not reason:
            issue("J4", f"not_reachable `{obligation_id}` needs a non-empty reason.")
        elif len(reason) > limits["max_reason_chars"]:
            issue("J6", f"not_reachable `{obligation_id}` reason exceeds {limits['max_reason_chars']} characters.")
        unreachable.add(obligation_id)
    missing = [item for item in obligation_node if item not in covered and item not in unreachable]
    if missing:
        issue("J4", f"Obligations neither observed by a view nor listed as not_reachable: {missing}.")
    planned = {str(view.get("scenario_id") or "") for view in views}
    for scenario_id in sorted(runnable_scenarios - planned):
        issue("J5", f"Runnable scenario `{scenario_id}` has no view.")
    return issues


def journey_patch_scope_issues(
    output: dict[str, object],
    *,
    previous_output: dict[str, object],
    affected_view_keys: set[str],
    affected_obligation_ids: set[str],
) -> list[dict[str, object]]:
    """J7: a patch may only change the typed views and obligations named by its feedback."""

    issues: list[dict[str, object]] = []
    previous_views = {str(item.get("key") or ""): item for item in _items(previous_output.get("views"))}
    candidate_views = {str(item.get("key") or ""): item for item in _items(output.get("views"))}

    def without_affected_refs(view: dict[str, object] | None) -> dict[str, object] | None:
        if view is None:
            return None
        normalized = dict(view)
        refs = view.get("obligation_refs")
        source_refs = refs if isinstance(refs, list) else []
        normalized["obligation_refs"] = [
            str(item)
            for item in source_refs
            if str(item) not in affected_obligation_ids
        ]
        return normalized

    for key, previous in previous_views.items():
        if key not in affected_view_keys and without_affected_refs(candidate_views.get(key)) != without_affected_refs(previous):
            issues.append(
                {
                    "gate": "J7",
                    "message": f"Unaffected view `{key}` must remain exactly unchanged in a journey patch.",
                }
            )

    def placements(plan: dict[str, object]) -> dict[str, dict[str, object]]:
        result: dict[str, dict[str, object]] = {}
        for view in _items(plan.get("views")):
            key = str(view.get("key") or "")
            refs = view.get("obligation_refs")
            for obligation_id in refs if isinstance(refs, list) else []:
                placement = result.setdefault(str(obligation_id), {"views": [], "not_reachable": []})
                placement_views = placement["views"]
                if isinstance(placement_views, list):
                    placement_views.append(key)
        for item in _items(plan.get("not_reachable")):
            obligation_id = str(item.get("obligation_id") or "")
            placement = result.setdefault(obligation_id, {"views": [], "not_reachable": []})
            unreachable = placement["not_reachable"]
            if isinstance(unreachable, list):
                unreachable.append(item)
        for placement in result.values():
            placement["views"] = sorted(str(item) for item in placement["views"])
        return result

    previous_placements = placements(previous_output)
    candidate_placements = placements(output)
    for obligation_id, previous in previous_placements.items():
        if obligation_id not in affected_obligation_ids and candidate_placements.get(obligation_id) != previous:
            issues.append(
                {
                    "gate": "J7",
                    "message": (
                        f"Unaffected obligation `{obligation_id}` must keep exactly the same view/not_reachable "
                        "assignment in a journey patch."
                    ),
                }
            )
    return issues


def merge_journey_patch(
    previous_output: dict[str, object],
    patch: dict[str, object],
    *,
    affected_obligation_ids: set[str],
) -> tuple[dict[str, object], list[dict[str, object]]]:
    """Apply a Planner patch to the accepted plan and return the full plan the gates check.

    A returned view replaces the accepted view with the same `key` or is added; `removed_view_keys` drops views;
    `not_reachable` is rewritten only for the affected obligations. Everything else stays exactly as accepted.
    """

    previous_views = _items(previous_output.get("views"))
    previous_keys = {str(view.get("key") or "") for view in previous_views}
    raw_removed = patch.get("removed_view_keys")
    removed = {str(item) for item in raw_removed} if isinstance(raw_removed, list) else set()
    replacement: dict[str, dict[str, object]] = {}
    added: list[dict[str, object]] = []
    for view in _items(patch.get("views")):
        key = str(view.get("key") or "")
        if key in previous_keys and key not in replacement:
            replacement[key] = view
        else:
            added.append(view)  # a repeated key reaches gate J1 on the merged plan
    issues = [
        {
            "gate": "J1",
            "message": f"removed_view_keys names `{key}`, which is "
            + ("also returned in `views`." if key in replacement else "not a view of the accepted plan."),
        }
        for key in sorted(removed)
        if key not in previous_keys or key in replacement
    ]
    patched = _items(patch.get("not_reachable"))
    rewritten = affected_obligation_ids | {str(item.get("obligation_id") or "") for item in patched}
    raw_observations = (previous_output.get("observations"), patch.get("observations"))
    observations = [str(item) for raw in raw_observations if isinstance(raw, list) for item in raw]
    merged = {
        "views": [
            *(
                replacement.get(str(view.get("key") or ""), view)
                for view in previous_views
                if str(view.get("key") or "") not in removed
            ),
            *added,
        ],
        "not_reachable": [
            *(
                item
                for item in _items(previous_output.get("not_reachable"))
                if str(item.get("obligation_id") or "") not in rewritten
            ),
            *patched,
        ],
        "observations": list(dict.fromkeys(observations)),
    }
    return merged, issues


def journey_views(output: dict[str, object]) -> list[dict[str, object]]:
    """Plan views with Harness-assigned ``BV-<digest>`` IDs (stable while key and steps are unchanged)."""

    views = []
    for view in _items(output.get("views")):
        rendered = capture_view({"view_id": "pending", "steps": view.get("steps")})
        digest = hashlib.sha256(
            json.dumps(
                {"scenario_id": view.get("scenario_id"), "key": view.get("key"), "steps": rendered["steps"]},
                sort_keys=True,
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        views.append(
            {
                "view_id": f"BV-{digest[:12]}",
                "key": str(view.get("key") or ""),
                "scenario_id": str(view.get("scenario_id") or ""),
                "design_node_ids": [str(item) for item in view.get("design_node_ids") or []],
                "obligation_refs": [str(item) for item in view.get("obligation_refs") or []],
                "steps": rendered["steps"],
            }
        )
    return views


def journey_step_failures(manifest: dict[str, object], views: list[dict[str, object]]) -> list[dict[str, object]]:
    """Every step_failed capture of a plan view, with the evidence the Planner patches from."""

    by_id = {str(view["view_id"]): view for view in views}
    failures = []
    for record in _items(manifest.get("records")):
        view = by_id.get(str(record.get("view_id")))
        if view is None or record.get("status") != "step_failed":
            continue
        index = int(record.get("step") or 0)
        steps = view["steps"]
        failures.append(
            {
                "view_key": view["key"],
                "view_id": view["view_id"],
                "viewport_id": record.get("viewport_id"),
                "step_index": index,
                "step": steps[index] if 0 <= index < len(steps) else None,
                "error": record.get("error"),
                "aria_at_failure": str(record.get("aria_snapshot") or "")[:MAX_FAILURE_ARIA_CHARS],
            }
        )
    return failures


def _step_issues(step: object, *, first: bool) -> list[tuple[str, str]]:
    if not isinstance(step, dict):
        return [("J1", "a step must be an object.")]
    action = step.get("action")
    if action not in STEP_ACTIONS:
        return [("J1", f"`action` must be one of {list(STEP_ACTIONS)}.")]
    problems: list[tuple[str, str]] = []
    if first != (action == "goto"):
        problems.append(("J1", "the first step, and only the first step, must be `goto`."))
    unknown = sorted(set(step) - _STEP_ALLOWED[action])
    if unknown:
        problems.append(("J1", f"`{action}` does not take {unknown}."))
    for field in _STEP_REQUIRED[action]:
        if not isinstance(step.get(field), str) or not step[field].strip():
            problems.append(("J1", f"`{action}` needs a non-empty string `{field}`."))
    locator = {key: step[key] for key in _LOCATOR_FIELDS if key in step}
    if action in {"click", "fill", "wait_for"} or locator:
        problems.extend(_locator_issues(locator, label=f"`{action}` locator"))
    if "observe" in step:
        problems.extend(_locator_issues(step["observe"], label=f"`{action}.observe`"))
    if action == "goto" and isinstance(step.get("path"), str) and not step["path"].startswith("/"):
        problems.append(("J1", "`goto.path` must start with `/`."))
    for field, limit in (
        ("path", "max_path_chars"),
        ("name", "max_name_chars"),
        ("text", "max_name_chars"),
        ("value", "max_value_chars"),
        ("key", "max_press_key_chars"),
    ):
        if isinstance(step.get(field), str) and len(step[field]) > JOURNEY_LIMITS[limit]:
            problems.append(("J6", f"`{field}` exceeds {JOURNEY_LIMITS[limit]} characters."))
    return problems


def _locator_issues(value: object, *, label: str) -> list[tuple[str, str]]:
    if not isinstance(value, dict):
        return [("J1", f"{label} must be an object.")]
    problems: list[tuple[str, str]] = []
    unknown = sorted(set(value) - _LOCATOR_FIELDS)
    if unknown:
        problems.append(("J1", f"{label} does not take {unknown}."))
    role = value.get("role")
    if role not in ARIA_ROLES:
        problems.append(("J1", f"{label} role `{role}` is not an ARIA role."))
    choices = [field for field in ("name", "text") if isinstance(value.get(field), str) and value[field].strip()]
    if len(choices) != 1 or set(value).intersection({"name", "text"}) != set(choices):
        problems.append(("J1", f"{label} needs exactly one non-empty string `name` or `text`."))
    within = value.get("within")
    if "within" in value and not (
        isinstance(within, dict)
        and set(within) == {"role", "name"}
        and within.get("role") in ARIA_ROLES
        and isinstance(within.get("name"), str)
        and within["name"].strip()
    ):
        problems.append(("J1", f"{label} `within` must be {{role, name}} with an ARIA role and non-empty name."))
    if "nth" in value and (
        not isinstance(value["nth"], int) or isinstance(value["nth"], bool) or value["nth"] < 0
    ):
        problems.append(("J1", f"{label} `nth` must be an integer >= 0."))
    for field in ("name", "text"):
        if isinstance(value.get(field), str) and len(value[field]) > JOURNEY_LIMITS["max_name_chars"]:
            problems.append(("J6", f"{label} `{field}` exceeds {JOURNEY_LIMITS['max_name_chars']} characters."))
    return problems


def _items(value: object) -> list[dict[str, object]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _image_reference(
    *,
    variant: str,
    manifest: dict[str, object],
    record: dict[str, object] | None,
    run_dir: Path,
    capture_key: str,
) -> dict[str, object]:
    screenshot = record.get("screenshot") if isinstance(record, dict) else None
    evidence_dir = str(manifest.get("evidence_dir") or "").strip()
    filename = str(screenshot.get("file") or "").strip() if isinstance(screenshot, dict) else ""
    sha256 = str(screenshot.get("sha256") or "").strip() if isinstance(screenshot, dict) else ""
    if not evidence_dir or not filename or not sha256 or record is None:
        raise HarnessError(
            "browser_image_selection_invalid",
            f"Capture {capture_key!r} in variant {variant!r} has no persisted screenshot reference.",
        )
    evidence_relative = Path(evidence_dir)
    if evidence_relative.is_absolute() or ".." in evidence_relative.parts or Path(filename).name != filename:
        raise HarnessError(
            "browser_image_selection_invalid",
            f"Capture {capture_key!r} has an unsafe persisted screenshot reference.",
        )
    root = run_dir.resolve()
    path = (root / evidence_relative / filename).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise HarnessError(
            "browser_image_selection_invalid",
            f"Capture {capture_key!r} resolves outside the run directory.",
        ) from exc
    step = int(record.get("step") or 0)
    return {
        "label": f"{variant}:{capture_key}:s{step:02d}",
        "path": str(path),
        "sha256": sha256,
    }


def _evidence_record(item: dict[str, object]) -> dict[str, object]:
    styles = [style for style in item.get("styles") or [] if isinstance(style, dict)]
    contrast = []
    for style in styles:
        props = style.get("props") if isinstance(style.get("props"), dict) else {}
        foreground, background = _hex_color(props.get("color")), _hex_color(props.get("effective-background-color"))
        if foreground and background:
            contrast.append({"target": style.get("target"), "ratio": round(_contrast(foreground, background), 2)})
    view_id = item.get("view_id")
    viewport_id = item.get("viewport_id")
    step = int(item.get("step") or 0)
    return {
        "capture_key": f"{view_id}:{viewport_id}:s{step:02d}",
        "view_id": view_id,
        "viewport_id": viewport_id,
        "step": step,
        "status": item.get("status"),
        "error": item.get("error"),
        **({"observe": item["observe"]} if isinstance(item.get("observe"), dict) else {}),
        "screenshot": item.get("screenshot"),
        "aria_snapshot": item.get("aria_snapshot"),
        "aria_truncated": bool(item.get("aria_truncated")),
        "focus": _focus(item.get("focus")),
        "layout": item.get("layout"),
        "styles": styles,
        "network": _items(item.get("network")),
        "network_truncated": bool(item.get("network_truncated")),
        "contrast": contrast,
    }


def _focus(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    match = _ARIA_LINE.match(str(value.get("aria") or ""))
    name = None
    if match and match.group(2) is not None:
        try:
            name = json.loads(f'"{match.group(2)}"')
        except json.JSONDecodeError:
            name = match.group(2)
    return {
        "role": match.group(1) if match else None,
        "name": name,
        "tag": value.get("tag"),
        "outline_style": value.get("outline_style"),
        "outline_width": value.get("outline_width"),
        "outline_color": value.get("outline_color"),
    }


def _hex_color(value: object) -> str | None:
    """#rrggbb for an opaque rgb()/rgba() computed color; anything else has no defined contrast here."""

    match = _RGB.match(str(value or "").strip())
    if not match or (match.group(4) is not None and float(match.group(4)) < 1):
        return None
    channels = [int(match.group(index)) for index in (1, 2, 3)]
    if any(channel > 255 for channel in channels):
        return None
    return "#" + "".join(f"{channel:02x}" for channel in channels)
