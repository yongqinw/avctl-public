import ActivityKit
import AppIntents
import SwiftUI
import UIKit
import WidgetKit

/// The island. Compact and minimal are display-only by Apple's rules; the
/// controls live in the expanded view (long-press the island) and on the
/// lock screen. The expanded layout borrows Apple Music's anatomy -- big
/// cover left, titles beside it, times flanking the bar, a big transport
/// row -- then diverges where this is a rack and not a phone: the power
/// button opens the transport row, and the mini's volume stands top-right
/// as a split-tap pill (plain "c" or fader "c+", config.yaml's choice).
struct NowPlayingLiveActivity: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: NowPlayingAttributes.self) { context in
            // No forced tint: nil hands the card to the system's own
            // translucent material -- the glass the native player sits on.
            // The island below keeps its always-black world; glass is a
            // lock-screen-only trait.
            LockScreenView(state: context.state)
                .activityBackgroundTint(nil)
                .activitySystemActionForegroundColor(.primary)
        } dynamicIsland: { context in
            DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    // Down and in from the card's corner curve -- flush
                    // against it, the cover's top-left gets shaved. 52pt
                    // stays: the 64pt experiment did not move the needle
                    // here (the system media card is its own world); the
                    // LOCK SCREEN kept the bigger cover instead.
                    CoverArt(state: context.state, size: 52)
                        .padding(.leading, 2)
                        .padding(.top, 6)
                }
                DynamicIslandExpandedRegion(.center) {
                    TrackTitles(state: context.state, alignment: .leading)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                DynamicIslandExpandedRegion(.trailing) {
                    // Off the edge, toward the titles -- flush right read
                    // as orphaned on the real hardware.
                    VolumeControl(state: context.state)
                        .padding(.trailing, 10)
                }
                DynamicIslandExpandedRegion(.bottom) {
                    // The vertical padding is what buys Music-app height:
                    // Apple sizes the card to its content, and Music's card
                    // breathes. Matching the breath matches the height.
                    VStack(spacing: 12) {
                        TimedProgress(state: context.state)
                        TransportRow(state: context.state)
                    }
                    .padding(.horizontal, 2)
                    .padding(.top, 8)
                    .padding(.bottom, 10)
                }
            } compactLeading: {
                // The same speaker pair as the expanded view: one identity
                // at every size, filled while the rack is actually playing.
                Image(systemName: context.state.playing
                      ? "hifispeaker.2.fill" : "hifispeaker.2")
                    .foregroundStyle(.white)
            } compactTrailing: {
                if context.state.failed {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .foregroundStyle(.yellow)
                } else {
                    Image(systemName: context.state.playing
                          ? "play.fill" : "pause.fill")
                        .foregroundStyle(.white)
                }
            } minimal: {
                Image(systemName: context.state.playing
                      ? "hifispeaker.2.fill" : "hifispeaker.2")
                    .foregroundStyle(.white)
            }
        }
    }
}

/// The cover from the local cache, or the speaker pair when the phone has
/// never seen this album -- disk or glyph, never a network wait, because a
/// widget process is not allowed one.
struct CoverArt: View {
    let state: NowPlayingAttributes.ContentState
    var size: CGFloat = 48
    var ink: Color = .white

    var body: some View {
        // Decoded bounded to THIS surface's drawn size: ActivityKit refuses
        // images larger than they render, and the island (52pt) and lock
        // screen (64pt) now draw the same stored cover at different sizes.
        if let cover = ArtworkStore.image(for: state.artworkKey, at: size) {
            Image(uiImage: cover)
                .resizable()
                .scaledToFill()
                .frame(width: size, height: size)
                .clipShape(RoundedRectangle(cornerRadius: size / 5.5))
        } else {
            Image(systemName: state.playing
                  ? "hifispeaker.2.fill" : "hifispeaker.2")
                .font(.title2)
                .foregroundStyle(ink)
                .frame(width: size, height: size)
        }
    }
}

struct TrackTitles: View {
    let state: NowPlayingAttributes.ContentState
    var alignment: HorizontalAlignment = .center
    /// HIG ladder defaults sized for the island (Music's own scale there:
    /// 17pt semibold over 13pt). The widget passes larger styles to match
    /// its larger art -- title2/subheadline on large, title3/footnote on
    /// medium -- staying on Apple's text styles so Dynamic Type holds.
    var titleFont: Font = .headline
    var bylineFont: Font = .footnote

    var body: some View {
        // Track over artist, nothing else -- Music's own lock-screen player
        // shows no album name, and matching its rhythm is the design.
        VStack(alignment: alignment, spacing: 2) {
            Text(state.track)
                .font(titleFont)
                .lineLimit(1)
            Text(state.artist)
                .font(bylineFont)
                .foregroundStyle(.secondary)
                .lineLimit(1)
        }
        .multilineTextAlignment(alignment == .leading ? .leading : .center)
    }
}

/// Elapsed and remaining flank the bar, Music-style. Playing, all three
/// advance on the phone's clock -- zero pushes spent on time passing.
struct TimedProgress: View {
    let state: NowPlayingAttributes.ContentState
    var ink: Color = .white

    var body: some View {
        // Music's exact grammar: elapsed plain on the left, remaining on
        // the right wearing a minus, both hugging the bar with the same
        // margin (frames align toward the bar so slack falls outward).
        if let start = state.trackStart, let end = state.trackEnd, end > start {
            HStack(spacing: 8) {
                Group {
                    if state.playing {
                        Text(timerInterval: start...end, countsDown: false,
                             showsHours: false)
                    } else {
                        Text(clock(played(start: start, end: end)))
                    }
                }
                .frame(width: 44, alignment: .trailing)
                if state.playing {
                    ProgressView(timerInterval: start...end, countsDown: false,
                                 label: {}, currentValueLabel: {})
                        .tint(ink)
                } else {
                    ProgressView(value: fraction(start: start, end: end))
                        .tint(ink.opacity(0.5))
                }
                HStack(spacing: 0) {
                    Text("−")
                    if state.playing {
                        Text(timerInterval: start...end, countsDown: true,
                             showsHours: false)
                    } else {
                        Text(clock(end.timeIntervalSince(start)
                                   - played(start: start, end: end)))
                    }
                }
                .frame(width: 44, alignment: .leading)
            }
            .font(.system(.caption2, design: .rounded))
            .monospacedDigit()
            .foregroundStyle(.secondary)
        }
    }

    private func played(start: Date, end: Date) -> TimeInterval {
        min(max(Date().timeIntervalSince(start), 0),
            end.timeIntervalSince(start))
    }

    private func fraction(start: Date, end: Date) -> Double {
        played(start: start, end: end) / end.timeIntervalSince(start)
    }

    private func clock(_ seconds: TimeInterval) -> String {
        let whole = Int(seconds.rounded())
        return String(format: "%d:%02d", whole / 60, whole % 60)
    }
}

/// Power opens the row -- the signature that says "this is the rack" --
/// then transport at Music-app scale.
struct TransportRow: View {
    let state: NowPlayingAttributes.ContentState
    /// White in the island's always-black world; the home widget passes
    /// .primary so light mode gets dark controls.
    var ink: Color = .white
    /// The island keeps the red all-off on the row's edge -- it has no
    /// deck. The LARGE widget hides it: its deck carries All off, and a
    /// second red power beside the transport is one fat-finger from dark.
    var showPower = true

    var body: some View {
        HStack(spacing: 0) {
            if showPower {
                Button(intent: AllOffIntent()) {
                    Image(systemName: "power")
                        .font(.body.weight(.semibold))
                        .foregroundStyle(.red.opacity(0.8))
                }
                .frame(maxWidth: .infinity)
            }
            Button(intent: PrevTrackIntent()) {
                Image(systemName: "backward.fill")
                    .font(.title2)
            }
            .frame(maxWidth: .infinity)
            Button(intent: PlayPauseIntent()) {
                Image(systemName: state.playing ? "pause.fill" : "play.fill")
                    .font(.largeTitle)
            }
            .frame(maxWidth: .infinity)
            Button(intent: NextTrackIntent()) {
                Image(systemName: "forward.fill")
                    .font(.title2)
            }
            .frame(maxWidth: .infinity)
            // Symmetry for the power button, so play sits dead center.
            if showPower {
                Color.clear.frame(maxWidth: .infinity, maxHeight: 1)
            }
        }
        .buttonStyle(.plain)
        .foregroundStyle(ink)
    }
}

/// The mini's volume as a tall pill, top-right where Music keeps its
/// waveform. Two styles, chosen by config.yaml phone.island_volume and
/// carried in the state ("c+" fader / "c" plain readout) -- a config edit
/// reskins this with no app rebuild. Both act the same way, because a
/// Live Activity permits only buttons: the pill's upper half steps up,
/// the lower half steps down; in "c+" the fill height is the level, live.
struct VolumeControl: View {
    let state: NowPlayingAttributes.ContentState
    var ink: Color = .white
    var width: CGFloat = 40
    var height: CGFloat = 52

    private var fader: Bool { (state.volumeStyle ?? "c") == "c+" }

    var body: some View {
        ZStack {
            if fader, let volume = state.volume, !state.muted {
                GeometryReader { geo in
                    Rectangle()
                        .fill(ink.opacity(0.22))
                        .frame(height: geo.size.height
                               * min(max(Double(volume) / 100, 0), 1))
                        .frame(maxWidth: .infinity, maxHeight: .infinity,
                               alignment: .bottom)
                }
            }
            VStack(spacing: 1) {
                if !fader {
                    Image(systemName: "chevron.up")
                        .font(.system(size: 7, weight: .bold))
                        .foregroundStyle(.tertiary)
                }
                if state.failed {
                    Image(systemName: "exclamationmark.triangle.fill")
                        .font(.caption)
                        .foregroundStyle(.yellow)
                } else if state.muted {
                    Image(systemName: "speaker.slash.fill")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                } else if let volume = state.volume {
                    Text("\(volume)")
                        .font(.system(.callout, design: .rounded).weight(.bold))
                        .monospacedDigit()
                } else {
                    // Volume unreadable: a quiet speaker, never a "--" that
                    // looks like something broke.
                    Image(systemName: "speaker.wave.2")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                if !fader {
                    Image(systemName: "chevron.down")
                        .font(.system(size: 7, weight: .bold))
                        .foregroundStyle(.tertiary)
                }
            }
            // The tap zones: invisible, split at the waist. Buttons are the
            // only interactivity Apple permits here -- this is the closest
            // legal physics to the swiper it resembles.
            VStack(spacing: 0) {
                Button(intent: MiniVolUpIntent()) {
                    Color.clear
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                Button(intent: MiniVolDownIntent()) {
                    Color.clear
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }
        }
        .frame(width: width, height: height)
        .background(RoundedRectangle(cornerRadius: width / 3)
            .fill(ink.opacity(0.08)))
        .clipShape(RoundedRectangle(cornerRadius: width / 3))
        .foregroundStyle(ink)
    }
}

private struct LockScreenView: View {
    let state: NowPlayingAttributes.ContentState
    // On glass the system decides light or dark; hardcoded white ink would
    // vanish over a bright wallpaper. The ink parameter the widget already
    // taught the shared views is exactly the lever.
    @Environment(\.colorScheme) private var scheme

    private var ink: Color { scheme == .dark ? .white : .primary }

    var body: some View {
        VStack(spacing: 8) {
            HStack(spacing: 12) {
                CoverArt(state: state, size: 64, ink: ink)
                TrackTitles(state: state, alignment: .leading)
                    .frame(maxWidth: .infinity, alignment: .leading)
                VolumeControl(state: state, ink: ink)
            }
            TimedProgress(state: state, ink: ink)
            TransportRow(state: state, ink: ink)
        }
        .padding(14)
        .foregroundStyle(ink)
    }
}
