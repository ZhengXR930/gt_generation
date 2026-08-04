<?php
$hash = unserialize(file_get_contents('/gt/poc'));
hash_update($hash, '');
