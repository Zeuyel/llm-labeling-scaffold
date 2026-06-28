import { matchRoute } from "./routeMatching.js";

export const ROUTES = [
  { pattern: "/", page: "tasks" },
  { pattern: "/settings", page: "settings" },
  { pattern: "/task/:id", page: "overview" },
  { pattern: "/task/:id/canvas", page: "canvas" },
  { pattern: "/task/:id/imports", page: "imports" },
  { pattern: "/task/:id/samples/:sampleId", page: "sampleDetail", activePage: "samples" },
  { pattern: "/task/:id/samples", page: "samples" },
  { pattern: "/task/:id/annotations", page: "annotations" },
  { pattern: "/task/:id/runs", page: "annotations" },
  { pattern: "/task/:id/jobs", page: "jobs" },
  { pattern: "/task/:id/gold", page: "gold" },
  { pattern: "/task/:id/models", page: "models" },
  { pattern: "/task/:id/archive", page: "archive" },
];

export function matchAppRoute(path) {
  for (const route of ROUTES) {
    const params = matchRoute(route.pattern, path);
    if (params) return { page: route.page, activePage: route.activePage || route.page, params };
  }
  return { page: "tasks", activePage: "tasks", params: {} };
}
