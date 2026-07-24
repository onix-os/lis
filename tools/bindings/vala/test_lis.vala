int main (string[] args) {
    var doc = new Lis.Document ();
    doc.hostname = "tron";
    doc.timezone = "Europe/Amsterdam";

    var user = new Lis.User ("bresilla");
    user.admin = true;

    assert (doc.lis == "0.1.0");
    assert (doc.hostname == "tron");
    assert (user.admin == true);

    stdout.printf ("Vala binding test passed! LIS Version: %s, Hostname: %s\n", doc.lis, doc.hostname);
    return 0;
}
