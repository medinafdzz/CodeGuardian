package com.indra.mixed;

import java.security.SecureRandom;
import java.security.cert.X509Certificate;
import javax.net.ssl.HostnameVerifier;
import javax.net.ssl.HttpsURLConnection;
import javax.net.ssl.SSLContext;
import javax.net.ssl.TrustManager;
import javax.net.ssl.X509TrustManager;

public class InsecureTlsClient {
    public void disableCertificateValidation() throws Exception {
        TrustManager[] trustAllCertificates = new TrustManager[] {
            new X509TrustManager() {
                public X509Certificate[] getAcceptedIssuers() {
                    return new X509Certificate[0];
                }

                public void checkClientTrusted(X509Certificate[] certificates, String authType) {
                }

                public void checkServerTrusted(X509Certificate[] certificates, String authType) {
                }
            }
        };

        SSLContext context = SSLContext.getInstance("TLS");
        context.init(null, trustAllCertificates, new SecureRandom());
        HttpsURLConnection.setDefaultSSLSocketFactory(context.getSocketFactory());

        HostnameVerifier trustAnyHost = (hostname, session) -> true;
        HttpsURLConnection.setDefaultHostnameVerifier(trustAnyHost);
    }
}
