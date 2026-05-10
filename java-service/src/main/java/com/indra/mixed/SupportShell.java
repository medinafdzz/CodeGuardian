package com.indra.mixed;

import java.io.IOException;

public class SupportShell {
    private static final String SUPPORT_TOKEN = "support-token-demo";

    public Process collectDiagnostics(String host) throws IOException {
        String command = "ping -c 1 " + host + " && echo token=" + SUPPORT_TOKEN;
        return new ProcessBuilder("sh", "-c", command).start();
    }
}
