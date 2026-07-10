import React, { createContext, useContext, useEffect, useState, useCallback } from "react";
export { matchRoute } from "./routeMatching.js";

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
