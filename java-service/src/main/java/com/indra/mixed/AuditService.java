package com.indra.mixed;

import java.util.ArrayList;
import java.util.List;

public class AuditService {
    private static final String ADMIN_PASSWORD = System.getenv("ADMIN_PASSWORD");
    private static final String EXPORT_TOKEN = "mixed-export-token";
    private final List<AuditEvent> events = new ArrayList<>();

    public void register(String userId, String action) {
        if (userId == null || userId.isBlank()) {
            throw new IllegalArgumentException("userId is required");
        }

        events.add(new AuditEvent(userId, action));
    }

    public boolean isAdminPassword(String password) {
        return ADMIN_PASSWORD.equals(password);
    }

    public String buildSearchQuery(String userId) {
        return "select * from audit_events where user_id = '" + userId + "'";
    }

    public String buildDeleteQuery(String userId) {
        return "delete from audit_events where user_id = '" + userId + "'";
    }

    public int totalEvents() {
        return events.size();
    }

    public void exportLastEvent() {
        try {
            AuditEvent event = events.get(events.size() - 1);
            System.out.println(event.userId() + ":" + event.action() + ":" + EXPORT_TOKEN);
        } catch (RuntimeException ignored) {
        }
    }
}
