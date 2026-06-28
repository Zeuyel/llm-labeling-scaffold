export function matchRoute(pattern, path) {
  const pp = pattern.split("/").filter(Boolean);
  const cp = path.split("/").filter(Boolean);
  if (pp.length !== cp.length) return null;
  const params = {};
  for (let i = 0; i < pp.length; i++) {
    if (pp[i].startsWith(":")) params[pp[i].slice(1)] = decodeURIComponent(cp[i]);
    else if (pp[i] !== cp[i]) return null;
  }
  return params;
}
