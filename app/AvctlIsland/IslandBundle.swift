import SwiftUI
import WidgetKit

@main
struct IslandBundle: WidgetBundle {
    var body: some Widget {
        NowPlayingLiveActivity()
        HomeNowPlayingWidget()
    }
}
