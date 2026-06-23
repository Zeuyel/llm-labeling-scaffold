import React, { createContext, useContext, useEffect, useState, useCallback } from "react";

const RouterCtx = createContext({ path: "/", navigate: () => {} });

function currentPath() {
  const h = window.location.hash || "#/";
  return h.replace(/^#/, "") || "/";
}

export function RouterProvider({ children }) {
  const [path, setPath] = useState(currentPath());
  useEffect(() => {
    const onHash = () => setPath(currentPath());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);
  const navigate = useCallback((to) => {
    window.location.hash = to.startsWith("#") ? to : `#${to}`;
  }, []);
  return <RouterCtx.Provider value={{ path, navigate }}>{children}</RouterCtx.Provider>;
}

export function useRouter() {
  return useContext(RouterCtx);
}

// matches "/task/:id/sample" against current path, returns params or null
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

export function Link({ to, className, children, title }) {
  const { navigate } = useRouter();
  return (
    <a
      href={`#${to}`}
      className={className}
      title={title}
      onClick={(e) => {
        e.preventDefault();
        navigate(to);
      }}
    >
      {children}
    </a>
  );
}
