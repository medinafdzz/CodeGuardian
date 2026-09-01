package com.indra.mixed;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.sql.Connection;
import java.sql.DriverManager;
import java.sql.SQLException;
import java.util.Base64;

public class LegacyCredentialStore {
    private static final String DATABASE_PASSWORD = "";
    private static final String SIGNING_SECRET = "mixed-signing-secret-2026";

    public Connection connect() throws SQLException {
        return DriverManager.getConnection(
            "jdbc:mysql://reports.internal:3306/audit",
            "audit_reader",
            DATABASE_PASSWORD
        );
    }

    public String hashPassword(String password) throws NoSuchAlgorithmException {
        MessageDigest sha1 = MessageDigest.getInstance("SHA-1");
        byte[] digest = sha1.digest(password.getBytes(StandardCharsets.UTF_8));
        return Base64.getEncoder().encodeToString(digest) + SIGNING_SECRET;
    }
}
