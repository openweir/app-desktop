// GENERATED from DESIGN.md by scripts/gen_theme.py. Do not edit; edit DESIGN.md.

pragma Singleton
import QtQuick
import OpenWeir

QtObject {
    // Main.qml binds this to the injected Appearance; the theme is light without one.
    property Appearance appearance
    readonly property bool dark: appearance?.dark ?? false

    // ---- colors: dark ? colors.dark.<role> : colors.<role> ----
    readonly property color brand: dark ? "#2EE6D2" : "#00B3A4"  // brand
    readonly property color primary: dark ? "#26D0BE" : "#00807A"  // primary
    readonly property color primaryHover: dark ? "#3ADBCA" : "#00736D"  // primary-hover
    readonly property color primaryPressed: dark ? "#4FE3D3" : "#006560"  // primary-pressed
    readonly property color textOnPrimary: dark ? "#00201D" : "#FFFFFF"  // on-primary
    readonly property color link: dark ? "#5EEBDC" : "#00665E"  // link
    readonly property color linkHover: dark ? "#9FF3EA" : "#004F49"  // link-hover
    readonly property color background: dark ? "#0B0F12" : "#F5F7F8"  // background
    readonly property color sidebar: dark ? "#101519" : "#EEF0F2"  // sidebar
    readonly property color sidebarHover: dark ? "#161C21" : "#E5E8EB"  // sidebar-hover
    readonly property color surface: dark ? "#161B20" : "#FFFFFF"  // surface
    readonly property color surfaceSubtle: dark ? "#1B2127" : "#F8F9FA"  // surface-subtle
    readonly property color surfaceSelected: dark ? "#0F3B38" : "#E3F7F5"  // surface-selected
    readonly property color controlTrack: dark ? "#05080A" : "#EFEFF0"  // control-track
    readonly property color divider: dark ? "#292D32" : "#EBECEC"  // divider
    readonly property color border: dark ? "#30363C" : "#DDE1E4"  // border
    readonly property color borderButton: dark ? "#414950" : "#C5CBD0"  // border-button
    readonly property color borderControl: dark ? "#7F8A93" : "#7C8790"  // border-control
    readonly property color text: dark ? "#F2F5F7" : "#0B0F12"  // text
    readonly property color textSecondary: dark ? "#B4BDC4" : "#4A5560"  // text-secondary
    readonly property color textTertiary: dark ? "#A6B0B8" : "#53606B"  // text-tertiary
    readonly property color textMuted: dark ? "#9AA4AC" : "#5F6A75"  // text-muted
    readonly property color success: dark ? "#32D583" : "#067647"  // success
    readonly property color warning: dark ? "#FDB022" : "#A04A05"  // warning
    readonly property color latencyPoor: dark ? "#F38B5E" : "#9A3412"  // latency-poor
    readonly property color danger: dark ? "#FF8077" : "#B42318"  // danger
    readonly property color info: dark ? "#62AFFF" : "#0B5CC2"  // info
    readonly property color chartUpload: dark ? "#F28A4B" : "#C2410C"  // chart-upload
    readonly property color scrim: dark ? "#80000000" : "#330B0F12"  // scrim
    readonly property color captionHover: dark ? "#0FFFFFFF" : "#0F0B0F12"  // caption-hover
    readonly property color captionPressed: dark ? "#0AFFFFFF" : "#0A0B0F12"  // caption-pressed
    readonly property color captionClose: dark ? "#C42B1C" : "#C42B1C"  // caption-close
    readonly property color shadowOverlay: dark ? "#66000000" : "#1F0B0F12"  // shadow-overlay
    readonly property color shadowSegment: dark ? "#52000000" : "#1F0B0F12"  // shadow-segment
    readonly property color shadowNav: dark ? "#3D000000" : "#140B0F12"  // shadow-nav

    // ---- typography (fontSize px, fontWeight 100-900, lineHeight multiplier) ----
    component TypeRole: QtObject {
        property string fontFamily
        property int fontSize
        property int fontWeight
        property real lineHeight: 1.0
    }
    component TypeScale: QtObject {
        readonly property TypeRole title: TypeRole {  // title
            fontFamily: "Inter"
            fontSize: 18
            fontWeight: 600
            lineHeight: 1.4
        }
        readonly property TypeRole bodyMd: TypeRole {  // body-md
            fontFamily: "Inter"
            fontSize: 14
            fontWeight: 400
            lineHeight: 1.5
        }
        readonly property TypeRole bodyMdStrong: TypeRole {  // body-md-strong
            fontFamily: "Inter"
            fontSize: 14
            fontWeight: 600
            lineHeight: 1.5
        }
        readonly property TypeRole bodySm: TypeRole {  // body-sm
            fontFamily: "Inter"
            fontSize: 13
            fontWeight: 400
            lineHeight: 1.5
        }
        readonly property TypeRole labelMd: TypeRole {  // label-md
            fontFamily: "Inter"
            fontSize: 14
            fontWeight: 500
            lineHeight: 1.2
        }
        readonly property TypeRole labelSm: TypeRole {  // label-sm
            fontFamily: "Inter"
            fontSize: 13
            fontWeight: 500
            lineHeight: 1.2
        }
        readonly property TypeRole labelSmStrong: TypeRole {  // label-sm-strong
            fontFamily: "Inter"
            fontSize: 13
            fontWeight: 600
            lineHeight: 1.2
        }
        readonly property TypeRole caption: TypeRole {  // caption
            fontFamily: "Inter"
            fontSize: 12
            fontWeight: 400
            lineHeight: 1.4
        }
        readonly property TypeRole sectionLabel: TypeRole {  // section-label
            fontFamily: "Inter"
            fontSize: 12
            fontWeight: 600
            lineHeight: 1.4
        }
        readonly property TypeRole dataMd: TypeRole {  // data-md
            fontFamily: "JetBrains Mono"
            fontSize: 13
            fontWeight: 400
            lineHeight: 1.5
        }
        readonly property TypeRole dataSm: TypeRole {  // data-sm
            fontFamily: "JetBrains Mono"
            fontSize: 12
            fontWeight: 400
            lineHeight: 1.6
        }
    }
    readonly property TypeScale type: TypeScale {}

    // ---- rounded ----
    readonly property int radiusXs: 4  // rounded.xs
    readonly property int radiusSm: 6  // rounded.sm
    readonly property int radiusMd: 8  // rounded.md
    readonly property int radiusFull: 9999  // rounded.full

    // ---- spacing ----
    readonly property int base: 4  // spacing.base
    readonly property int space1: 4  // spacing.1
    readonly property int space2: 8  // spacing.2
    readonly property int space3: 12  // spacing.3
    readonly property int space4: 16  // spacing.4
    readonly property int space5: 20  // spacing.5
    readonly property int space6: 24  // spacing.6
    readonly property int space8: 32  // spacing.8
    readonly property int space10: 40  // spacing.10
    readonly property int pageX: 32  // spacing.page-x
    readonly property int pageBottom: 40  // spacing.page-bottom
    readonly property int sidebarWidth: 216  // spacing.sidebar-width
    readonly property int groupColumnMin: 400  // spacing.group-column-min
    readonly property int nameMinWidth: 160  // spacing.name-min-width
    readonly property int windowDefaultWidth: 1280  // spacing.window-default-width
    readonly property int windowDefaultHeight: 800  // spacing.window-default-height
    readonly property int windowMinWidth: 960  // spacing.window-min-width
    readonly property int windowMinHeight: 640  // spacing.window-min-height
    readonly property int titlebarHeight: 48  // spacing.titlebar-height
    readonly property int titlebarPageTop: 6  // spacing.titlebar-page-top
    readonly property int captionButtonWidth: 46  // spacing.caption-button-width
    readonly property int sidebarTopMacos: 36  // spacing.sidebar-top-macos
    readonly property int sidebarTopWindows: 16  // spacing.sidebar-top-windows
    readonly property int controlSm: 32  // spacing.control-sm
    readonly property int controlMd: 36  // spacing.control-md
    readonly property int rowSingle: 44  // spacing.row-single
    readonly property int rowDouble: 52  // spacing.row-double
    readonly property int rowHeader: 32  // spacing.row-header
    readonly property int overlayGap: 4  // spacing.overlay-gap
    readonly property int overlayMargin: 8  // spacing.overlay-margin
    readonly property int overlayShadowY: 4  // spacing.overlay-shadow-y
    readonly property int overlayShadowBlur: 16  // spacing.overlay-shadow-blur
    readonly property int menuMinWidth: 180  // spacing.menu-min-width
    readonly property int menuMaxWidth: 480  // spacing.menu-max-width
    readonly property int popoverMinWidth: 240  // spacing.popover-min-width
    readonly property int popoverMaxWidth: 360  // spacing.popover-max-width
    readonly property int dialogWidth: 400  // spacing.dialog-width
    readonly property int dialogWidthWide: 640  // spacing.dialog-width-wide
    readonly property int dialogWidthEditor: 840  // spacing.dialog-width-editor
    readonly property int installerWidth: 640  // spacing.installer-width
    readonly property int installerHeight: 440  // spacing.installer-height
    readonly property int listHeaderHeight: 28  // spacing.list-header-height
    readonly property int insertionLine: 2  // spacing.insertion-line
    readonly property int editorMinHeight: 240  // spacing.editor-min-height

    // ---- opacity ----
    readonly property real disabledOpacity: 0.4  // opacity.disabled
}
