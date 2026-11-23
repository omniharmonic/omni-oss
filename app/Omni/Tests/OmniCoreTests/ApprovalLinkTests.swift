import Foundation
@testable import OmniCore
import XCTest

@MainActor
final class ApprovalLinkTests: XCTestCase {
    func testAnApprovalLinkKeepsItsIDAndAsksToFocusItOnce() {
        var queue = ExternalNavigationQueue()
        XCTAssertTrue(queue.receive(URL(string: "omni://approval/apr_0123456789abcdef01234567")!))
        XCTAssertEqual(queue.take(signedIn: true, unlocked: true, navigationReady: true), .needsYou)
        XCTAssertEqual(queue.takeFocusedApproval(), "apr_0123456789abcdef01234567")
        XCTAssertNil(queue.takeFocusedApproval())
        XCTAssertNil(queue.takeFocusedCard())
        XCTAssertTrue(queue.receive(URL(string: "omni://approval/apr_a")!))
        XCTAssertTrue(queue.receive(URL(string: "omni://thread/t1")!))
        XCTAssertEqual(queue.take(signedIn: true, unlocked: true, navigationReady: true), .thread("t1"))
        XCTAssertNil(queue.takeFocusedApproval(), "a later thread tap replaces the approval focus")
        XCTAssertFalse(queue.receive(URL(string: "omni://approval/a%2Fb")!))
    }

    func testALoneDraftOpensNeedsYouFocusedOnIt() async {
        let service = FakeService()
        service.pending(.success([Fixture.approval("apr_lone", threadId: nil)]))
        let session = SessionModel(service: service, sleep: noSleep) {}
        await session.approvals.refresh()
        await session.openApproval("apr_lone")
        XCTAssertEqual(session.destination, .needsYou)
        XCTAssertEqual(session.approvals.focusedApprovalID, "apr_lone")
    }

    func testALinkedApprovalOpensItsChatEvenBeforeTheQueueLoaded() async {
        let service = FakeService()
        service.approvalReads(.success(Fixture.approval("apr_chat", threadId: "omni-approval-apr_chat")))
        let session = SessionModel(service: service, sleep: noSleep) {}
        await session.openApproval("apr_chat")
        XCTAssertEqual(session.destination, .thread("omni-approval-apr_chat"))
        XCTAssertNil(session.approvals.focusedApprovalID)
    }

    func testAnUnknownApprovalStillLandsOnNeedsYou() async {
        let service = FakeService()
        let session = SessionModel(service: service, sleep: noSleep) {}
        await session.openApproval("apr_gone")
        XCTAssertEqual(session.destination, .needsYou)
        XCTAssertEqual(session.approvals.focusedApprovalID, "apr_gone")
    }

    func testLoneDraftsComeBeforeDecide() {
        XCTAssertEqual(NeedsYouLayout.sections(drafts: 2, decide: 3, approve: 1, motion: 1), [.drafts, .decide, .approve, .motion])
        XCTAssertEqual(NeedsYouLayout.sections(drafts: 0, decide: 1, approve: 0, motion: 2), [.decide, .motion])
        XCTAssertEqual(NeedsYouLayout.sections(drafts: 1, decide: 0, approve: 0, motion: 0), [.drafts])
    }
}
