import XCTest

final class CoreSetupUITests: XCTestCase {
    override func setUpWithError() throws {
        continueAfterFailure = false
    }

    func testFreshInstallPairsWithLanCoreOnIPhoneAndIPad() throws {
        let environment = ProcessInfo.processInfo.environment
        guard let coreURL = environment["AVCTL_E2E_CORE_URL"],
              let coreToken = environment["AVCTL_E2E_CORE_TOKEN"] else {
            throw XCTSkip("set AVCTL_E2E_CORE_URL and AVCTL_E2E_CORE_TOKEN")
        }

        let app = XCUIApplication()
        app.launchArguments += ["-AppleLanguages", "(en)",
                                "-AppleLocale", "en_US"]

        addUIInterruptionMonitor(withDescription: "Local network") { alert in
            for label in ["Allow", "OK"] where alert.buttons[label].exists {
                alert.buttons[label].tap()
                return true
            }
            return false
        }

        app.launch()
        XCTAssertTrue(app.staticTexts["Connect your Core"]
            .waitForExistence(timeout: 10))
        if app.frame.width >= 700 {
            XCTAssertTrue(app.staticTexts["Choose services"].exists)
            XCTAssertTrue(app.staticTexts["Configure panels"].exists)
            XCTAssertTrue(app.staticTexts["Verify access"].exists)
        }

        let address = app.textFields["https://your-core.tailnet.ts.net"]
        XCTAssertTrue(address.exists)
        address.tap()
        address.typeText(coreURL)

        let token = app.secureTextFields["Bearer token (optional with Tailscale)"]
        XCTAssertTrue(token.exists)
        token.tap()
        token.typeText(coreToken)

        app.buttons["Pair with Core"].tap()
        app.tap() // gives any local-network permission alert to the monitor

        XCTAssertTrue(app.webViews.firstMatch.waitForExistence(timeout: 20),
                      "pairing should replace native setup with Core web UI")
        XCTAssertTrue(app.staticTexts["Check this Mac"]
            .waitForExistence(timeout: 20),
            "the bearer must unlock the web setup, not merely show a web view")

        app.terminate()
        app.launch()
        XCTAssertTrue(app.staticTexts["Check this Mac"]
            .waitForExistence(timeout: 20),
            "the paired bearer session must survive an app relaunch")
    }
}
