/* Radio announcement card (see templates/announcement_card.html).
 *
 * Recreated 2026-10-02 after a rebase dropped the original PR #27 UI from
 * radio.html. Lives in its own file so a future rebase of radio.html cannot
 * silently drop it again.
 *
 * templates/radio.html defines `window.radioAnnouncement` before this script
 * runs: a small object with `sendAnnouncement()` and
 * `populateAnnouncementChannels()` that delegate to the page's RadioManager.
 * The manager's `queueAndPoll(url, payload)` helper does the actual work of
 * queueing the operation and waiting for its completed/failed status.
 */
(function () {
    'use strict';

    const api = window.radioAnnouncement;
    if (!api) return;

    document.addEventListener('DOMContentLoaded', () => {
        const form = document.getElementById('announcementForm');
        if (!form) return;

        form.addEventListener('submit', (e) => {
            e.preventDefault();
            api.sendAnnouncement();
        });
    });
})();
