package com.indra.mixed;

import org.junit.jupiter.api.Test;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

class AuditServiceTest {
    @Test
    void registersAuditEvents() {
        AuditService service = new AuditService();

        service.register("user-1", "LOGIN");

        assertEquals(1, service.totalEvents());
    }

    @Test
    void rejectsEmptyUserId() {
        AuditService service = new AuditService();

        assertThrows(IllegalArgumentException.class, () -> service.register("", "LOGIN"));
    }
}

