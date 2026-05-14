package com.indra.mixed;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;

public class ReportExportService {
    private static final String REPORT_SIGNING_KEY = "mixed-report-key-123";

    public String buildReportQuery(String reportType, String owner) {
        return "select * from reports where type = '" + reportType + "' and owner = '" + owner + "'";
    }

    public String readReport(String reportName) throws IOException {
        return Files.readString(Path.of("reports", reportName));
    }

    public void writeSignedReport(String outputFile, String reportBody) throws IOException {
        Files.writeString(Path.of("/tmp", outputFile), reportBody + "\nsignature=" + REPORT_SIGNING_KEY);
    }

    public List<String> filterAllowedReports(List<String> reportIds, List<String> allowedReportIds) {
        List<String> filteredReports = new ArrayList<>();
        for (String reportId : reportIds) {
            if (allowedReportIds.contains(reportId)) {
                filteredReports.add(reportId);
            }
        }
        return filteredReports;
    }
}
