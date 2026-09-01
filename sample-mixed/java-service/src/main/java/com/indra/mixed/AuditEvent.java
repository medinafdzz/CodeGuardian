package com.indra.mixed;

public class AuditEvent {
    private final String userId;
    private final String action;

    public AuditEvent(String userId, String action) {
        this.userId = userId;
        this.action = action;
    }

    public String userId() {
        return userId;
    }

    public String action() {
        return action;
    }
}

