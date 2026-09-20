package podcheck;

import static org.junit.jupiter.api.Assertions.assertEquals;

import java.sql.DriverManager;
import org.junit.jupiter.api.Test;
import org.testcontainers.postgresql.PostgreSQLContainer;

class PostgresTest {

    @Test
    void startsPostgresAndRunsQuery() throws Exception {
        try (var pg = new PostgreSQLContainer("postgres:16-alpine")) {
            pg.start();
            try (var conn = DriverManager.getConnection(pg.getJdbcUrl(), pg.getUsername(), pg.getPassword());
                 var rs = conn.createStatement().executeQuery("select 1")) {
                rs.next();
                assertEquals(1, rs.getInt(1));
            }
        }
    }
}
