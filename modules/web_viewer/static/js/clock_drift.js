"use strict";

// Shared drift badges for Contacts and Time. Verification status is separate.
(() => {
    function badge(contact) {
        const status = contact.clock_drift_status || 'unknown';
        const hasDrift = contact.clock_drift_seconds !== null
            && contact.clock_drift_seconds !== undefined
            && Number.isFinite(Number(contact.clock_drift_seconds));
        const drift = hasDrift ? Math.abs(Number(contact.clock_drift_seconds)) : 0;

        if (!hasDrift) {
            return ' <span class="badge clock-drift-badge bg-secondary"'
                + ' data-bs-toggle="tooltip" data-bs-placement="top"'
                 + ' title="No drift data — no recent packet with a timestamp">N/D</span>';
        }
        if (status === 'stale') {
            return ` <span class="badge clock-drift-badge bg-secondary" title="Mesure ancienne : ${full(drift)}">${compact(drift)}</span>`;
        }

        const compactText = compact(drift);
        const fullText = full(drift);

        if (status === 'in_sync') {
            return ` <span class="badge clock-drift-badge bg-success"`
                + ` data-bs-toggle="tooltip" data-bs-placement="top"`
                + ` title="In sync — drift ${fullText}">${compactText}</span>`;
        }
        if (status === 'warning') {
            return ` <span class="badge clock-drift-badge bg-warning text-dark"`
                + ` data-bs-toggle="tooltip" data-bs-placement="top"`
                + ` title="Drift approaching threshold — ${fullText}"><i class="fas fa-clock"></i> ${compactText}</span>`;
        }
        return ` <span class="badge clock-drift-badge bg-danger"`
            + ` data-bs-toggle="tooltip" data-bs-placement="top"`
            + ` title="Device clock out of sync — drift ${fullText}"><i class="fas fa-clock"></i> ${compactText}</span>`;
    }

    function compact(seconds) {
        const s = Math.abs(Math.round(seconds));
        if (s < 60) return `${s}s`;
        const minutes = Math.floor(s / 60);
        if (minutes < 60) return `${minutes}m${s % 60 ? `${s % 60}s` : ''}`;
        const hours = Math.floor(minutes / 60);
        if (hours < 24) return `${hours}h${minutes % 60 ? `${minutes % 60}m` : ''}`;
        const days = Math.floor(hours / 24);
        return `${days}d${hours % 24 ? `${hours % 24}h` : ''}`;
    }

    function full(seconds) {
        const s = Math.abs(Math.round(seconds));
        const days = Math.floor(s / 86400);
        const hours = Math.floor((s % 86400) / 3600);
        const minutes = Math.floor((s % 3600) / 60);
        const secs = s % 60;
        const parts = [];
        if (days > 0) parts.push(`${days}d`);
        if (hours > 0) parts.push(`${hours}h`);
        if (minutes > 0) parts.push(`${minutes}m`);
        parts.push(`${secs}s`);
        return parts.join(' ');
    }

    globalThis.MeshClockBadges = Object.freeze({ badge, compact, full });
})();
