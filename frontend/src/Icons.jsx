import React from 'react'

const paths = {
  compass: <><circle cx="12" cy="12" r="9"/><path d="m15.5 8.5-2 5-5 2 2-5 5-2Z"/></>,
  sparkles: <><path d="m12 3 2.2 6.8L21 12l-6.8 2.2L12 21l-2.2-6.8L3 12l6.8-2.2L12 3Z"/><path d="m20 2 .6 1.4L22 4l-1.4.6L20 6l-.6-1.4L18 4l1.4-.6L20 2Z"/></>,
  plus: <path d="M12 5v14M5 12h14"/>,
  send: <><path d="m21 3-7 18-3-8-8-3 18-7Z"/><path d="m21 3-10 10"/></>,
  stop: <rect x="6" y="6" width="12" height="12" rx="2"/>,
  chat: <><path d="M21 11.5a8.5 8.5 0 0 1-8.5 8.5 9 9 0 0 1-4-.9L3 21l1.9-5.5a9 9 0 0 1-.9-4A8.5 8.5 0 0 1 12.5 3H13a8.5 8.5 0 0 1 8 8v.5Z"/></>,
  history: <><path d="M3 11a9 9 0 1 1 2.7 7M3 4v7h7"/><path d="M12 7v5l3 2"/></>,
  beaker: <><path d="M9 3h6M10 3v6L4.5 18a2 2 0 0 0 1.7 3h11.6a2 2 0 0 0 1.7-3L14 9V3M8 14h8"/></>,
  refresh: <><path d="M20 7v5h-5M4 17v-5h5"/><path d="M6.1 7a7 7 0 0 1 11.5-2L20 8M4 16l2.4 3A7 7 0 0 0 18 17"/></>,
  external: <><path d="M15 3h6v6M21 3l-9 9M10 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-5"/></>,
  chevron: <path d="m9 5 7 7-7 7"/>,
  close: <path d="m6 6 12 12M6 18 18 6"/>,
  trash: <><path d="M3 6h18M9 6V3h6v3M5 6l1 15h12l1-15M10 10v7M14 10v7"/></>,
  code: <><path d="m8 7-5 5 5 5m8-10 5 5-5 5M14 4l-4 16"/></>,
  link: <><path d="m10 13 4-4M8 15l-2 2a3.5 3.5 0 0 1-5-5l5-5a3.5 3.5 0 0 1 5 0m2 2 2-2a3.5 3.5 0 0 1 5 5l-5 5a3.5 3.5 0 0 1-5 0" transform="translate(1 0)"/></>,
  database: <><ellipse cx="12" cy="5" rx="8" ry="3"/><path d="M4 5v14c0 4 16 4 16 0V5M4 12c0 4 16 4 16 0"/></>,
  shield: <><path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3Z"/><path d="m8 12 3 3 5-6"/></>,
  search: <><circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/></>,
  calculator: <><rect x="5" y="3" width="14" height="18" rx="2"/><path d="M8 7h8M8 11h1m3 0h1m3 0h.1M8 15h1m3 0h1m3 0h.1M8 18h1m3 0h1m3 0h.1"/></>,
  activity: <path d="M2 12h4l3-8 5 16 3-8h5"/>,
  check: <path d="m5 12 4 4L19 6"/>,
  map: <><path d="m3 5 6-2 6 2 6-2v16l-6 2-6-2-6 2V5ZM9 3v16M15 5v16"/></>,
  wallet: <><rect x="3" y="5" width="18" height="15" rx="2"/><path d="M3 8V5l14-3v3M16 11h5v5h-5v-5Z"/></>,
  plane: <path d="m21 3-7 18-3-7-7-3L21 3ZM11 14 21 3M4 20l3-3"/>,
  train: <><rect x="5" y="3" width="14" height="15" rx="3"/><path d="M5 10h14M12 3v7M8 14h.1M16 14h.1M8 18l-2 3M16 18l2 3"/></>,
  hotel: <><path d="M3 21V8h7V3h11v18M3 21h18M6 12h1m-1 4h1M14 7h1m2 0h1m-4 4h1m2 0h1m-4 4h1m2 0h1M11 21v-5h3v5"/></>,
  file: <><path d="M14 2H5v20h14V7l-5-5ZM14 2v6h5M8 12h8M8 16h6"/></>,
  menu: <path d="M4 6h16M4 12h16M4 18h16"/>,
}
export function Icon({ name, size = 18, ...rest }) {
  return <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.65" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" {...rest}>{paths[name] || paths.activity}</svg>
}
