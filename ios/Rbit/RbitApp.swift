import SwiftUI

@main
struct RbitApp: App {
    @StateObject private var app = AppState()

    var body: some Scene {
        WindowGroup {
            RootView()
                .environmentObject(app)
                .preferredColorScheme(app.preferredColorScheme)
                .onOpenURL { url in
                    app.handlePairingURL(url)
                }
        }
    }
}
