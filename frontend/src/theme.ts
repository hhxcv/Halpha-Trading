import { createTheme } from "@mui/material/styles";

export const surfaceFrameSx = {
  border: 1,
  borderColor: "divider",
  borderRadius: "14px",
  bgcolor: "background.paper",
} as const;

const semanticColors = {
  success: { main: "#2F856D", text: "#236553", soft: "#EEF7F3", border: "#BFDCCD", contrast: "#FFFFFF" },
  warning: { main: "#F59E0B", text: "#7A4100", soft: "#FFF7DD", border: "#EBCB72", contrast: "#111827" },
  error: { main: "#B54743", text: "#873631", soft: "#FFF1EF", border: "#E7B8B3", contrast: "#FFFFFF" },
  info: { main: "#526AA8", text: "#3A4F82", soft: "#F0F4FF", border: "#CBD6F3", contrast: "#FFFFFF" },
} as const;

const marketColors = {
  up: "#00805A",
  down: "#C93434",
} as const;

const colors = {
  accent: "#FFD43B",
  accentHover: "#FFDF5A",
  accentBorder: "#E0B800",
  accentText: "#C2410C",
  background: "#F7F8FB",
  surface: "#FFFFFF",
  surfaceMuted: "#F2F4F7",
  surfaceInverse: "#EEF1F5",
  border: "#DBE1EA",
  borderStrong: "#B9C3D0",
  text: "#111827",
  textSecondary: "#4B5563",
  textMuted: "#8892A1",
} as const;

export const theme = createTheme({
  palette: {
    mode: "light",
    background: { default: colors.background, paper: colors.surface },
    primary: { main: colors.accent, dark: colors.accentBorder, contrastText: colors.text },
    secondary: { main: colors.accentText, contrastText: "#FFFFFF" },
    info: { main: semanticColors.info.main, dark: semanticColors.info.text, contrastText: semanticColors.info.contrast },
    warning: { main: semanticColors.warning.main, dark: semanticColors.warning.text, contrastText: semanticColors.warning.contrast },
    error: { main: semanticColors.error.main, dark: semanticColors.error.text, contrastText: semanticColors.error.contrast },
    success: { main: semanticColors.success.main, dark: semanticColors.success.text, contrastText: semanticColors.success.contrast },
    text: { primary: colors.text, secondary: colors.textSecondary, disabled: colors.textMuted },
    divider: colors.border,
  },
  shape: { borderRadius: 10 },
  spacing: 8,
  typography: {
    fontFamily: 'Inter, Geist, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
    fontSize: 14,
    h1: { fontSize: "clamp(1.625rem, 2.5vw, 2rem)", fontWeight: 650, lineHeight: 1.12, letterSpacing: 0 },
    h2: { fontSize: "1.0625rem", fontWeight: 650, lineHeight: 1.35, letterSpacing: 0 },
    h3: { fontSize: "0.9375rem", fontWeight: 650, lineHeight: 1.35, letterSpacing: 0 },
    body1: { fontSize: "0.875rem", lineHeight: 1.5, letterSpacing: 0 },
    body2: { fontSize: "0.8125rem", lineHeight: 1.45, letterSpacing: 0 },
    caption: { fontSize: "0.75rem", lineHeight: 1.4, letterSpacing: 0 },
    button: { fontSize: "0.8125rem", textTransform: "none", fontWeight: 700, letterSpacing: 0 },
    overline: { fontSize: "0.75rem", lineHeight: 1.2, fontWeight: 650, letterSpacing: 0, textTransform: "none" },
  },
  components: {
    MuiCssBaseline: {
      styleOverrides: {
        html: { backgroundColor: colors.background },
        ":root": {
          "--halpha-accent-text": colors.accentText,
          "--halpha-selection": colors.accent,
          "--halpha-selection-text": colors.text,
          "--halpha-surface": colors.surface,
          "--halpha-semantic-success": semanticColors.success.text,
          "--halpha-semantic-warning": semanticColors.warning.text,
          "--halpha-semantic-error": semanticColors.error.text,
          "--halpha-semantic-info": semanticColors.info.text,
          "--halpha-market-up": marketColors.up,
          "--halpha-market-down": marketColors.down,
        },
        ':root[data-halpha-market-color-scheme="RED_UP_GREEN_DOWN"]': {
          "--halpha-market-up": marketColors.down,
          "--halpha-market-down": marketColors.up,
        },
        body: {
          minWidth: 320,
          backgroundColor: colors.background,
          fontVariantNumeric: "tabular-nums",
          scrollbarColor: `${colors.borderStrong} ${colors.surfaceMuted}`,
        },
        ".mono": {
          fontFamily: '"JetBrains Mono", "IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace',
          fontVariantNumeric: "tabular-nums",
        },
        ".market-tone-up": { color: "var(--halpha-market-up) !important", fontWeight: 800 },
        ".market-tone-down": { color: "var(--halpha-market-down) !important", fontWeight: 800 },
        "::selection": { backgroundColor: "#FFE98A", color: colors.text },
        "*:focus-visible": { outline: `3px solid rgba(255, 212, 59, .42)`, outlineOffset: 2 },
        "button, input, select, textarea": { letterSpacing: 0 },
        "@media (prefers-reduced-motion: reduce)": {
          "*, *::before, *::after": {
            animationDuration: "0.01ms !important",
            animationIterationCount: "1 !important",
            transitionDuration: "0.01ms !important",
            scrollBehavior: "auto !important",
          },
        },
      },
    },
    MuiAppBar: {
      styleOverrides: {
        root: {
          color: colors.text,
          backgroundColor: "rgba(253, 253, 254, .96)",
          backgroundImage: "none",
          borderBottom: `1px solid ${colors.border}`,
          boxShadow: "none",
          backdropFilter: "blur(8px)",
        },
      },
    },
    MuiPaper: {
      defaultProps: { elevation: 0 },
      styleOverrides: { root: { backgroundImage: "none" } },
    },
    MuiDrawer: {
      styleOverrides: {
        paper: {
          color: colors.text,
          backgroundColor: colors.surfaceInverse,
          backgroundImage: "none",
          borderColor: colors.border,
        },
      },
    },
    MuiListItemButton: {
      styleOverrides: {
        root: {
          minHeight: 42,
          borderRadius: 10,
          color: colors.textSecondary,
          transition: "background-color 150ms cubic-bezier(.2,.8,.2,1), color 150ms cubic-bezier(.2,.8,.2,1), transform 90ms cubic-bezier(.2,.8,.2,1)",
          "&:hover": { backgroundColor: "#E2E6EE", color: colors.text },
          "&:active": { transform: "scale(.985)" },
          "&.Mui-focusVisible": {
            outline: "3px solid rgba(255, 212, 59, .72)",
            outlineOffset: 2,
            backgroundColor: "#E2E6EE",
          },
          "&.Mui-selected": {
            backgroundColor: colors.accent,
            color: colors.text,
            "&:hover": { backgroundColor: colors.accentHover },
            "&.Mui-focusVisible": { outlineColor: colors.text, backgroundColor: colors.accent },
          },
        },
      },
    },
    MuiListItemIcon: { styleOverrides: { root: { color: "inherit" } } },
    MuiButton: {
      styleOverrides: {
        root: {
          minHeight: 40,
          paddingInline: 14,
          borderRadius: 10,
          boxShadow: "none",
          transition: "background-color 150ms cubic-bezier(.2,.8,.2,1), border-color 150ms cubic-bezier(.2,.8,.2,1), color 150ms cubic-bezier(.2,.8,.2,1), transform 90ms cubic-bezier(.2,.8,.2,1)",
          "&:active:not(.Mui-disabled)": { transform: "scale(.985)" },
          "&.MuiButton-containedPrimary": {
            border: 0,
            color: colors.text,
            backgroundColor: colors.accent,
            "&:hover": { color: colors.text, backgroundColor: colors.accentHover, boxShadow: "none" },
          },
          "&.MuiButton-outlined": {
            borderColor: colors.border,
            color: colors.text,
            backgroundColor: colors.surface,
            "&:hover": { borderColor: colors.borderStrong, backgroundColor: colors.surfaceMuted },
          },
          "&.MuiButton-text": {
            color: colors.textSecondary,
            "&:hover": { color: colors.text, backgroundColor: colors.surfaceMuted },
          },
          "&.MuiButton-outlinedInfo": {
            borderColor: semanticColors.info.border,
            color: semanticColors.info.text,
            "&:hover": { borderColor: semanticColors.info.main, backgroundColor: semanticColors.info.soft },
          },
          "&.MuiButton-outlinedSuccess": {
            borderColor: semanticColors.success.border,
            color: semanticColors.success.text,
            "&:hover": { borderColor: semanticColors.success.main, backgroundColor: semanticColors.success.soft },
          },
          "&.MuiButton-outlinedWarning": {
            borderColor: semanticColors.warning.border,
            color: semanticColors.warning.text,
            "&:hover": { borderColor: semanticColors.warning.main, backgroundColor: semanticColors.warning.soft },
          },
          "&.MuiButton-outlinedError": {
            borderColor: semanticColors.error.border,
            color: semanticColors.error.text,
            "&:hover": { borderColor: semanticColors.error.main, backgroundColor: semanticColors.error.soft },
          },
          "&.MuiButton-textInfo": { color: semanticColors.info.text },
          "&.MuiButton-textSuccess": { color: semanticColors.success.text },
          "&.MuiButton-textWarning": { color: semanticColors.warning.text },
          "&.MuiButton-textError": { color: semanticColors.error.text },
          "&.MuiButton-containedInfo": { backgroundColor: semanticColors.info.main, color: semanticColors.info.contrast },
          "&.MuiButton-containedSuccess": { backgroundColor: semanticColors.success.main, color: semanticColors.success.contrast },
          "&.MuiButton-containedWarning": { backgroundColor: semanticColors.warning.main, color: semanticColors.warning.contrast },
          "&.MuiButton-containedError": { backgroundColor: semanticColors.error.main, color: semanticColors.error.contrast },
          "&.MuiButton-containedInfo:hover": { backgroundColor: semanticColors.info.text },
          "&.MuiButton-containedSuccess:hover": { backgroundColor: semanticColors.success.text },
          "&.MuiButton-containedWarning:hover": { backgroundColor: "#D98A00" },
          "&.MuiButton-containedError:hover": { backgroundColor: semanticColors.error.text },
          "&.Mui-disabled": {
            color: colors.textSecondary,
            borderColor: "#E2E8F0",
            backgroundColor: "#F1F5F9",
            opacity: 1,
          },
        },
      },
    },
    MuiLink: {
      styleOverrides: {
        root: {
          color: colors.accentText,
          fontWeight: 650,
          textUnderlineOffset: 3,
          "&:hover": { color: "#963400" },
        },
      },
    },
    MuiIconButton: {
      styleOverrides: {
        root: {
          width: 40,
          height: 40,
          border: `1px solid ${colors.border}`,
          borderRadius: 10,
          color: colors.text,
          backgroundColor: colors.surface,
          transition: "background-color 150ms cubic-bezier(.2,.8,.2,1), border-color 150ms cubic-bezier(.2,.8,.2,1), transform 90ms cubic-bezier(.2,.8,.2,1)",
          "&:hover": { borderColor: colors.borderStrong, backgroundColor: colors.surfaceMuted },
          "&:active": { transform: "scale(.985)" },
        },
      },
    },
    MuiTabs: {
      styleOverrides: {
        root: {
          width: "max-content",
          maxWidth: "100%",
          minHeight: 42,
          padding: 3,
          border: `1px solid ${colors.border}`,
          borderRadius: 10,
          backgroundColor: colors.surfaceMuted,
        },
        indicator: { display: "none" },
        list: { gap: 3 },
      },
    },
    MuiTab: {
      styleOverrides: {
        root: {
          minWidth: 0,
          minHeight: 34,
          padding: "7px 10px",
          borderRadius: 6,
          color: colors.textSecondary,
          fontSize: 13,
          fontWeight: 700,
          lineHeight: 1.2,
          textTransform: "none",
          transition: "background-color 150ms cubic-bezier(.2,.8,.2,1), color 150ms cubic-bezier(.2,.8,.2,1), transform 90ms cubic-bezier(.2,.8,.2,1)",
          "&:hover": { backgroundColor: colors.surface, color: colors.text },
          "&:active": { transform: "scale(.985)" },
          "&.Mui-focusVisible": { outline: "3px solid rgba(255, 212, 59, .72)", outlineOffset: 2 },
          "&.Mui-selected": {
            backgroundColor: colors.accent,
            color: colors.text,
            "&.Mui-focusVisible": { outlineColor: colors.text },
          },
        },
      },
    },
    MuiOutlinedInput: {
      styleOverrides: {
        root: {
          minHeight: 40,
          borderRadius: 10,
          backgroundColor: colors.surface,
          "& .MuiOutlinedInput-notchedOutline": { borderColor: colors.border },
          "&:hover .MuiOutlinedInput-notchedOutline": { borderColor: colors.borderStrong },
          "&.Mui-focused .MuiOutlinedInput-notchedOutline": { borderColor: colors.accentBorder, borderWidth: 1 },
        },
        input: { paddingBlock: 10 },
      },
    },
    MuiInputLabel: {
      styleOverrides: {
        root: {
          color: colors.textSecondary,
          "&.Mui-focused": { color: colors.accentText },
        },
      },
    },
    MuiMenuItem: {
      styleOverrides: {
        root: {
          minHeight: 40,
          borderRadius: 6,
          marginInline: 4,
          "&.Mui-selected": { backgroundColor: "#FFF4B8", "&:hover": { backgroundColor: "#FFEA87" } },
        },
      },
    },
    MuiChip: {
      styleOverrides: {
        root: {
          minHeight: 24,
          borderRadius: 999,
          fontSize: 12,
          fontWeight: 700,
          "&.MuiChip-colorSuccess": { color: semanticColors.success.text },
          "&.MuiChip-outlinedSuccess": { color: semanticColors.success.text, borderColor: semanticColors.success.border },
          "&.MuiChip-colorWarning": { color: semanticColors.warning.text },
          "&.MuiChip-outlinedWarning": { color: semanticColors.warning.text, borderColor: semanticColors.warning.border },
        },
        outlined: { backgroundColor: colors.surface },
      },
    },
    MuiAlert: {
      styleOverrides: {
        root: {
          border: `1px solid ${colors.border}`,
          borderRadius: 10,
          boxShadow: "none",
          alignItems: "center",
          "& .MuiAlert-icon": { color: "inherit", opacity: .9 },
          "& .MuiAlert-action": { alignItems: "center", paddingTop: 0 },
          "& .MuiAlert-action .MuiButton-root": { color: "inherit !important" },
        },
        standard: {
          "&.MuiAlert-colorInfo": { borderColor: semanticColors.info.border, backgroundColor: semanticColors.info.soft, color: semanticColors.info.text },
          "&.MuiAlert-colorSuccess": { borderColor: semanticColors.success.border, backgroundColor: semanticColors.success.soft, color: semanticColors.success.text },
          "&.MuiAlert-colorWarning": { borderColor: semanticColors.warning.border, backgroundColor: semanticColors.warning.soft, color: semanticColors.warning.text },
          "&.MuiAlert-colorError": { borderColor: semanticColors.error.border, backgroundColor: semanticColors.error.soft, color: semanticColors.error.text },
        },
        outlined: {
          backgroundColor: colors.surface,
          "&.MuiAlert-colorInfo": { borderColor: semanticColors.info.border, color: semanticColors.info.text },
          "&.MuiAlert-colorSuccess": { borderColor: semanticColors.success.border, color: semanticColors.success.text },
          "&.MuiAlert-colorWarning": { borderColor: semanticColors.warning.border, color: semanticColors.warning.text },
          "&.MuiAlert-colorError": { borderColor: semanticColors.error.border, color: semanticColors.error.text },
        },
        filled: {
          "&.MuiAlert-colorInfo": { borderColor: semanticColors.info.main, backgroundColor: semanticColors.info.main, color: semanticColors.info.contrast },
          "&.MuiAlert-colorSuccess": { borderColor: semanticColors.success.main, backgroundColor: semanticColors.success.main, color: semanticColors.success.contrast },
          "&.MuiAlert-colorWarning": { borderColor: semanticColors.warning.main, backgroundColor: semanticColors.warning.main, color: semanticColors.warning.contrast },
          "&.MuiAlert-colorError": { borderColor: semanticColors.error.main, backgroundColor: semanticColors.error.main, color: semanticColors.error.contrast },
        },
      },
    },
    MuiDivider: { styleOverrides: { root: { borderColor: colors.border } } },
    MuiLinearProgress: {
      styleOverrides: {
        root: { height: 7, borderRadius: 999, backgroundColor: "#E4E8EF" },
        bar: { borderRadius: 999, backgroundColor: colors.accentBorder },
      },
    },
    MuiTooltip: {
      styleOverrides: {
        tooltip: {
          border: `1px solid ${colors.border}`,
          borderLeft: `3px solid ${colors.accent}`,
          borderRadius: 10,
          color: colors.text,
          backgroundColor: colors.surface,
          boxShadow: "0 18px 48px rgba(42,60,95,.16)",
          fontSize: 12,
        },
      },
    },
    MuiDialog: {
      styleOverrides: {
        paper: { border: `1px solid ${colors.border}`, borderRadius: 18, boxShadow: "0 18px 48px rgba(42,60,95,.16)" },
      },
    },
  },
});
