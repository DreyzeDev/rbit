import SwiftUI

struct RootView: View {
    @EnvironmentObject private var app: AppState
    @Environment(\.scenePhase) private var scenePhase

    var body: some View {
        TabView(selection: $app.selectedTab) {
            HomeView()
                .tabItem {
                    Label("Новый", systemImage: "plus")
                }
                .tag(AppTab.new)

            HistoryView()
                .tabItem {
                    Label("История", systemImage: "clock.arrow.circlepath")
                }
                .tag(AppTab.history)

            SettingsView()
                .tabItem {
                    Label("Настройки", systemImage: "person.crop.circle")
                }
                .tag(AppTab.settings)
        }
        .tint(Color.rbitAccent)
        .fullScreenCover(item: $app.presentedSession) { route in
            ChatView(route: route)
                .environmentObject(app)
        }
        .sheet(isPresented: $app.isAddServerPresented) {
            AddServerView()
                .environmentObject(app)
        }
        .overlay(alignment: .top) {
            if let toast = app.toast {
                ToastView(toast: toast)
                    .padding(.top, 6)
                    .transition(.move(edge: .top).combined(with: .opacity))
                    .zIndex(10)
            }
        }
        .animation(.spring(response: 0.34, dampingFraction: 0.86), value: app.toast)
        .onChange(of: scenePhase) { _, phase in
            if phase == .active {
                app.appDidBecomeActive()
            }
        }
    }
}
